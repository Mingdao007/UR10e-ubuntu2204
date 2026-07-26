#!/usr/bin/env python3
"""Reduce the latest canonical Step5d attempt into one route-neutral status."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import Any, Mapping

TOOLS = Path(__file__).resolve().parent
REPOSITORY_ROOT = TOOLS.parents[2]
RUNTIME_SOURCE = REPOSITORY_ROOT / "src" / "ur10e_experiment_runtime"
sys.path.insert(0, str(RUNTIME_SOURCE))
sys.path.insert(0, str(TOOLS))

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
from step5d_autotune_v3.bridge_admission import (
    BridgeAdmissionError,
    release_contract_reference,
    resolve_bridge_admission,
)
from step5d_autotune_v3.delivery_observation import (
    DeliveryObservationError,
    resolve_delivery_observation,
)
from step5d_autotune_v3.release_identity import (
    ReleaseIdentityError,
    load_current_release,
)
from step5d_autotune_v3.release_certificate import ReleaseCertificateError
from step5d_autotune_v3.release_contract import ReleaseContractError
from step5d_bridge_authority import (
    BridgeAuthorityError,
    load_current as load_owner_authority,
)

STATUS_SCHEMA = "step5d.bridge/governed-status-v3"
CLAIM_SCHEMA = "step5d.bridge/readiness-claim-v1"
AUTHORITY_RELATIVE = Path("runs/step5d_bridge_authority")
CLAIM_TTL_NS = 5_000_000_000
STATUS_CLAIM_MAX_AGE_NS = 1_000_000_000
PLAY_PROMPT = "PLAY_PROMPT"


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
            "release_contract_proven": False,
            "bridge_process_alive": False,
            "bridge_heartbeat_fresh": False,
            "canonical_attempt_bound": False,
            "play_prompt_ready": False,
        },
        "milestones": {
            "liveness": {
                "bridge_process_alive": False,
                "bridge_heartbeat_fresh": False,
            },
            "authorization": {
                "canonical_attempt_bound": False,
                "single_writer": False,
                "lease_valid": False,
            },
            "acceptance_certificate": {
                "play_prompt_ready": False,
                "release_contract_proven": False,
                "admission": [],
            },
            "acceptance": {
                "play_prompt_ready": False,
                "release_contract_proven": False,
                "admission": [],
            },
        },
        "capabilities": {"play_prompt": False},
        "next_operator_action": None,
        "blocker": {"class": blocker_class, "reason_codes": [reason], "evidence": []},
        "next_action": {
            "NO_CANONICAL_LAUNCH_ATTEMPT": "start_canonical_bridge",
            "LAUNCH_ATTEMPT_FAILED": "inspect_launch_attempt_evidence",
            "LAUNCH_ATTEMPT_CANCELLED": "start_new_canonical_attempt",
            "ROUTE_NOT_RESOLVED": "collect_fresh_route_snapshot",
            "ROUTE_RUNTIME_NOT_OBSERVED": "wait_for_route_runtime_observation",
            "LAUNCH_ATTEMPT_BINDING_INVALID": "repair_launch_attempt_binding",
            "CURRENT_RELEASE_INVALID": "repair_current_release_before_retry",
            "RELEASE_CERTIFICATE_MISSING": "run_revalidate_current",
            "DELIVERY_REVALIDATION_REQUIRED": "run_revalidate_current",
            "MANUAL_V2_ARCHIVED": "start_current_autotune_v3_bridge",
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


def _resolve_pre_attempt_status(root: Path) -> dict[str, Any]:
    try:
        release = load_current_release(root)
    except (OSError, ValueError, ReleaseIdentityError):
        return _base_status("CURRENT_RELEASE_INVALID", attempt=None)
    try:
        release_contract_reference(root, release)
    except (
        OSError,
        BridgeAdmissionError,
        ReleaseCertificateError,
        ReleaseContractError,
    ):
        return _base_status("RELEASE_CERTIFICATE_MISSING", attempt=None)
    try:
        delivery_path, delivery = resolve_delivery_observation(
            root,
            release=release,
        )
    except (OSError, ValueError, DeliveryObservationError):
        return _base_status("DELIVERY_REVALIDATION_REQUIRED", attempt=None)
    status = _base_status("NO_CANONICAL_LAUNCH_ATTEMPT", attempt=None)
    status["predicates"]["controller_fresh_get"] = True
    status["predicates"]["release_contract_proven"] = True
    status["milestones"]["acceptance"]["release_contract_proven"] = True
    status["blocker"] = {"class": None, "reason_codes": [], "evidence": []}
    status["next_action"] = "run_bridge_live"
    try:
        admission_path, admission = resolve_bridge_admission(
            root,
            release=release,
        )
    except BridgeAdmissionError:
        return status
    if admission["state"] == "BRIDGE_START_READY":
        evidence = [
            {
                "role": "bridge_admission",
                "path": admission_path.relative_to(root).as_posix(),
                "delivery_observation": delivery_path.relative_to(root).as_posix(),
                "transaction_id": delivery["transaction_id"],
            }
        ]
        status["blocker"] = {"class": None, "reason_codes": [], "evidence": evidence}
    return status


def _owner_authority_state(
    root: Path,
    attempt: Mapping[str, Any],
) -> dict[str, Any] | None:
    owner = attempt["bindings"]["resource_owner"]
    pid = owner.get("pid")
    starttime = owner.get("starttime_ticks")
    metadata = attempt["bindings"].get("resource_owner_metadata")
    metadata_fields = metadata if isinstance(metadata, Mapping) else {}
    worktree = metadata_fields.get("worktree_root")
    repository_head = metadata_fields.get("repository_head")
    launch_basis_path = metadata_fields.get("launch_basis_path")
    launch_basis_sha256 = metadata_fields.get("launch_basis_sha256")
    owner_resource_id = owner.get("resource_id")
    resource_id = metadata_fields.get("resource_id")
    if resource_id is None:
        resource_id = owner_resource_id
    has_global_metadata = bool(metadata) or resource_id is not None
    if not isinstance(pid, int) or not isinstance(starttime, int):
        return None
    try:
        authority = load_owner_authority(
            None,
            attempt_id=attempt.get("attempt_id"),
            owner_pid=pid,
            owner_starttime_ticks=starttime,
            worktree_root=worktree,
            repository_head=repository_head,
            launch_basis_path=launch_basis_path,
            launch_basis_sha256=launch_basis_sha256,
            resource_id=resource_id,
        )
    except (BridgeAuthorityError, OSError, ValueError):
        authority = None
    if has_global_metadata:
        return authority
    if authority is not None:
        return authority
    legacy_root = root / AUTHORITY_RELATIVE
    try:
        return load_owner_authority(legacy_root)
    except (BridgeAuthorityError, OSError, ValueError):
        return None


def _owner_authority_metadata(
    authority: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if authority is None:
        return None
    return {
        "attempt_id": authority.get("attempt_id"),
        "owner": authority.get("owner"),
        "worktree_root": authority.get("worktree_root"),
        "repository_head": authority.get("repository_head"),
        "launch_basis_path": authority.get("launch_basis_path"),
        "launch_basis_sha256": authority.get("launch_basis_sha256"),
    }


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
    metadata_fields = bindings.get("resource_owner_metadata")
    metadata_fields = metadata_fields if isinstance(metadata_fields, Mapping) else {}
    completed = attempt.get("state") == "COMPLETED"
    current_starttime = read_proc_starttime_ticks(pid)
    if not completed and current_starttime != starttime:
        return False
    authority = _owner_authority_state(root, attempt)
    if authority is None:
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
    metadata_fields = {
        "worktree_root": metadata_fields.get("worktree_root", owner.get("worktree_root")),
        "repository_head": metadata_fields.get(
            "repository_head", owner.get("repository_head")
        ),
        "launch_basis_path": metadata_fields.get(
            "launch_basis_path", owner.get("launch_basis_path")
        ),
        "launch_basis_sha256": metadata_fields.get(
            "launch_basis_sha256", owner.get("launch_basis_sha256")
        ),
    }
    for key, value in metadata_fields.items():
        if value is None:
            continue
        if authority.get(key) != value:
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
    attempt_bound = bool(
        attempt.get("route") == "autotune_v3"
        and status.get("predicates", {}).get("lease_valid") is True
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
        return _resolve_pre_attempt_status(root)
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
    route = attempt.get("route")
    if route == "UNKNOWN":
        return _base_status("ROUTE_NOT_RESOLVED", attempt=attempt)
    if not _binding_valid(root, attempt):
        return _base_status("LAUNCH_ATTEMPT_BINDING_INVALID", attempt=attempt)
    campaign_root = Path(attempt["bindings"]["campaign_root"])
    if route == "manual_v2":
        return _base_status("MANUAL_V2_ARCHIVED", attempt=attempt)
    status = resolve_governed_status(root, campaign_root)
    owner_authority = _owner_authority_state(root, attempt)
    owner_authority_metadata = _owner_authority_metadata(owner_authority)
    if owner_authority_metadata is not None:
        status["owner_authority"] = owner_authority_metadata
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
        or predicates.get("release_contract_proven") is not True
    ):
        raise ValueError("machine status does not authorize the requested readiness claim")


def _require_capability(
    status: Mapping[str, Any],
    capability: str,
    *,
    now_ns: int | None = None,
) -> None:
    if capability != PLAY_PROMPT:
        raise ValueError("unsupported readiness capability")
    _require_readiness_state(
        status,
        "WAITING_FOR_PLAY",
        now_ns=now_ns,
    )
    capabilities = status.get("capabilities")
    if (
        not isinstance(capabilities, Mapping)
        or capabilities.get("play_prompt") is not True
    ):
        raise ValueError("machine status does not authorize the requested capability")


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
    parser.add_argument(
        "--assert-capability",
        choices=(PLAY_PROMPT,),
    )
    args = parser.parse_args(argv)
    if args.assert_state is not None and args.assert_capability is not None:
        parser.error("assert-state and assert-capability cannot be combined")
    try:
        status = resolve_status(args.experiment_root)
        payload = status
        if args.assert_state is not None:
            payload = readiness_claim(status, args.assert_state)
        elif args.assert_capability is not None:
            _require_capability(status, args.assert_capability)
            payload = readiness_claim(status, "WAITING_FOR_PLAY")
    except Exception as exc:
        print(f"Step5d governed status unavailable: {type(exc).__name__}:{exc}", file=sys.stderr)
        return 3
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
