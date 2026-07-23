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

sys.path.insert(0, str(Path(__file__).resolve().parent))

from step5d_autotune_v3.governance import (
    EXTERNAL_REASON_CODES,
    LAUNCH_ATTEMPT_SCHEMA,
    LaunchAttemptAttestationError,
    LaunchAttemptPointerError,
    PHYSICAL_REASON_CODES,
    ObservationPointerError,
    ObservedAttestationError,
    load_current_observation,
    load_current_launch_attempt,
    read_proc_starttime_ticks,
    resolve_governed_status,
)
from step5d_autotune_v3.public_state import project_status
from step5d_bridge_authority import (
    BridgeAuthorityError,
    load_current as load_owner_authority,
)


STATUS_SCHEMA = "step5d.bridge/governed-status-v2"
CLAIM_SCHEMA = "step5d.bridge/readiness-claim-v1"
AUTHORITY_RELATIVE = Path("runs/step5d_bridge_authority")
CLAIM_TTL_NS = 5_000_000_000
STATUS_CLAIM_MAX_AGE_NS = 1_000_000_000


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


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
            "canonical_attempt_bound": False,
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


def _v3_attempt_binding_valid(
    attempt: Mapping[str, Any],
    campaign_root: Path,
    status: Mapping[str, Any],
) -> bool:
    try:
        attestation, _pointer = load_current_observation(campaign_root)
        process_reference = attestation["process"]["evidence"]
        process_path = campaign_root / str(process_reference["path"])
        process_bytes = process_path.read_bytes()
        process_evidence = json.loads(process_bytes.decode("utf-8"))
    except (
        OSError,
        UnicodeError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        ObservationPointerError,
        ObservedAttestationError,
    ):
        return False
    owner = attempt["bindings"]["resource_owner"]
    processes = process_evidence.get("processes")
    if not isinstance(processes, list):
        return False
    by_role = {
        row.get("role"): row for row in processes if isinstance(row, Mapping)
    }
    canonical = by_role.get("canonical_launcher")
    bridge = by_role.get("bridge_wrapper")
    release = status.get("release")
    attestation_view = status.get("attestation")
    lease = status.get("campaign_lease")
    return bool(
        process_path.is_file()
        and not process_path.is_symlink()
        and hashlib.sha256(process_bytes).hexdigest()
        == process_reference.get("sha256")
        and process_evidence.get("schema")
        == "step5d.autotune-v3/process-observation-evidence-v1"
        and attestation.get("run_id") == attempt.get("attempt_id")
        and attestation.get("campaign_id")
        == (lease.get("campaign_id") if isinstance(lease, Mapping) else None)
        and attestation.get("bindings", {}).get("manifest_sha256")
        == attempt.get("manifest_sha256")
        and isinstance(release, Mapping)
        and release.get("sha256") == attempt.get("manifest_sha256")
        and isinstance(attestation_view, Mapping)
        and attestation_view.get("run_id") == attempt.get("attempt_id")
        and isinstance(canonical, Mapping)
        and canonical.get("pid") == owner.get("pid")
        and canonical.get("starttime_ticks") == owner.get("starttime_ticks")
        and isinstance(bridge, Mapping)
        and bridge.get("pid") == attestation["process"]["bridge_pid"]
        and bridge.get("starttime_ticks")
        == attestation["process"]["bridge_starttime_ticks"]
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
    attempt_bound = bool(
        (
            attempt.get("route") == "manual_v2"
            and status.get("predicates", {}).get("canonical_attempt_bound") is True
        )
        or (
            attempt.get("route") == "autotune_v3"
            and status.get("predicates", {}).get("lease_valid") is True
        )
    )
    status["predicates"]["canonical_attempt_bound"] = attempt_bound
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
        status["predicates"]["canonical_attempt_bound"] = False
        status["predicates"]["play_prompt_ready"] = False
        status["next_action"] = "inspect_outcome_or_start_new_canonical_attempt"
        return status
    if not attempt_bound:
        status["predicates"]["play_prompt_ready"] = False
        if status.get("state") in {"WAITING_FOR_IDENTITY_PLAY", "WAITING_FOR_PLAY"}:
            status["state"] = "BRIDGE_ALIVE_NO_ARM"
        status["next_action"] = "restart_through_canonical_bridge"
    return status


def _resolve_detailed_status(experiment_root: Path) -> dict[str, Any]:
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
            from step5d_manual_status import read_run_status

            manual = read_run_status(campaign_root)
        except (ImportError, OSError, ValueError, json.JSONDecodeError):
            return _base_status("ROUTE_RUNTIME_NOT_OBSERVED", attempt=attempt)
        if manual.get("launch_attempt_id") != attempt.get("attempt_id"):
            return _base_status("LAUNCH_ATTEMPT_BINDING_INVALID", attempt=attempt)
        status = {
            "schema": STATUS_SCHEMA,
            "route": "manual_v2",
            "generated_at_unix_ns": time.time_ns(),
            "state": manual.get("state"),
            "predicates": {
                "offline_proven": manual.get("offline_proven") is True,
                "production_path_qualified": manual.get("offline_proven") is True,
                "bridge_process_alive": manual.get("bridge_heartbeat") is True,
                "bridge_heartbeat_fresh": manual.get("bridge_heartbeat") is True,
                "controller_preflight_valid": manual.get("controller_preflight_valid")
                is True,
                "canonical_attempt_bound": manual.get("canonical_attempt_bound") is True,
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
    if not _v3_attempt_binding_valid(attempt, campaign_root, status):
        return _base_status("LAUNCH_ATTEMPT_BINDING_INVALID", attempt=attempt)
    return _apply_attempt_gate(status, attempt)


def resolve_status(experiment_root: Path) -> dict[str, Any]:
    """Expose six computed states and one-window detailed phase compatibility."""

    return project_status(_resolve_detailed_status(experiment_root))


def _require_readiness_state(
    status: Mapping[str, Any],
    required_state: str,
    *,
    now_ns: int | None = None,
) -> None:
    if required_state not in {"WAITING_FOR_IDENTITY_PLAY", "WAITING_FOR_PLAY"}:
        raise ValueError("only pre-Play readiness states can be asserted")
    predicates = status.get("predicates")
    generated_at = status.get("generated_at_unix_ns")
    observed_now = time.time_ns() if now_ns is None else now_ns
    if (
        status.get("compatibility_phase", status.get("state")) != required_state
        or not isinstance(predicates, Mapping)
        or isinstance(generated_at, bool)
        or not isinstance(generated_at, int)
        or not 0 <= observed_now - generated_at <= STATUS_CLAIM_MAX_AGE_NS
        or predicates.get("play_prompt_ready") is not True
        or predicates.get("canonical_attempt_bound") is not True
        or predicates.get("offline_proven") is not True
        or predicates.get("production_path_qualified") is not True
        or (
            status.get("route") == "manual_v2"
            and predicates.get("controller_preflight_valid") is not True
        )
    ):
        raise ValueError("machine status does not authorize the requested readiness claim")


def readiness_claim(status: Mapping[str, Any], required_state: str) -> dict[str, Any]:
    issued = time.time_ns()
    _require_readiness_state(status, required_state, now_ns=issued)
    status_sha = hashlib.sha256(canonical_json_bytes(dict(status))).hexdigest()
    return {
        "schema": CLAIM_SCHEMA,
        "state": required_state,
        "attempt_id": status["launch_attempt"]["attempt_id"],
        "status_sha256": status_sha,
        "issued_at_unix_ns": issued,
        "expires_at_unix_ns": issued + CLAIM_TTL_NS,
    }


def verify_readiness_claim(
    status: Mapping[str, Any],
    claim: Mapping[str, Any],
    *,
    now_ns: int | None = None,
) -> dict[str, Any]:
    required = {
        "schema",
        "state",
        "attempt_id",
        "status_sha256",
        "issued_at_unix_ns",
        "expires_at_unix_ns",
    }
    if set(claim) != required or claim.get("schema") != CLAIM_SCHEMA:
        raise ValueError("readiness claim schema differs")
    state = claim.get("state")
    if not isinstance(state, str):
        raise ValueError("readiness claim state differs")
    issued = claim.get("issued_at_unix_ns")
    expires = claim.get("expires_at_unix_ns")
    observed_now = time.time_ns() if now_ns is None else now_ns
    expected_status_sha = hashlib.sha256(
        canonical_json_bytes(dict(status))
    ).hexdigest()
    if (
        claim.get("attempt_id") != status["launch_attempt"]["attempt_id"]
        or claim.get("status_sha256") != expected_status_sha
        or isinstance(issued, bool)
        or not isinstance(issued, int)
        or isinstance(expires, bool)
        or not isinstance(expires, int)
        or expires - issued != CLAIM_TTL_NS
        or not issued <= observed_now < expires
    ):
        raise ValueError("readiness claim is stale or bound to different status")
    _require_readiness_state(status, state, now_ns=observed_now)
    return dict(claim)


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
