"""Machine-derived governance state for the canonical Step5d V3 bridge."""

from __future__ import annotations

from dataclasses import dataclass
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence

from .delivery_observation import (
    DeliveryObservationError,
    MAX_AGE_NS as CONTROLLER_FRESH_GET_MAX_AGE_NS,
    fresh_get_provenance,
)


OBSERVED_ATTESTATION_SCHEMA = "step5d.autotune-v3/observed-attestation-v1"
CURRENT_OBSERVATION_POINTER_SCHEMA = (
    "step5d.autotune-v3/current-observation-pointer-v1"
)
CAMPAIGN_LEASE_SCHEMA = "step5d.autotune-v3/campaign-lease-v1"
GOVERNED_STATUS_SCHEMA = "step5d.autotune-v3/governed-status-v1"
QUALIFICATION_CURRENT_POINTER_SCHEMA = (
    "step5d.autotune-v3/qualification-current-pointer-v1"
)
LAUNCH_ATTEMPT_SCHEMA = "step5d.autotune-v3/launch-attempt-v1"
CURRENT_LAUNCH_ATTEMPT_POINTER_SCHEMA = (
    "step5d.autotune-v3/current-launch-attempt-pointer-v1"
)
FSM_TRANSITION_ACTOR = "launcher_supervisor"

LAUNCH_ATTEMPT_STATES = ("STARTED", "FAILED")
LAUNCH_ATTEMPT_PHASES = (
    "runtime_gate",
    "status_before",
    "tp_build",
    "release_candidate",
    "qualification",
    "tp_delivery",
    "status_after_delivery",
    "campaign_prepare",
    "preflight",
    "live_handoff",
)

FSM_STATES = (
    "OFFLINE_PROVEN",
    "WAITING_FOR_IDENTITY_PLAY",
    "BENCH_READY",
    "WAITING_FOR_PLAY",
    "RUNNING",
)
BLOCKER_CLASSES = ("BLOCKED_EXTERNAL", "INTERNAL", "PHYSICAL", "UNKNOWN")

BRIDGE_HEARTBEAT_MAX_AGE_NS = 1_000_000_000
CONTROLLER_OBSERVATION_MAX_AGE_NS = 1_000_000_000
RTDE_OBSERVATION_MAX_AGE_NS = 250_000_000
KUNWEI_OBSERVATION_MAX_AGE_NS = 250_000_000
MAILBOX_OBSERVATION_MAX_AGE_NS = 1_000_000_000

TP_RUNTIME_IDENTITY_FIELDS = {
    "schema",
    "program_id",
    "protocol_id",
    "protocol_version",
    "digest_hi",
    "digest_lo",
    "script_basis_sha256",
    "script_artifact_sha256",
    "registers",
}
TP_RUNTIME_IDENTITY_SCHEMA = "step5d.autotune-v3/tp-runtime-identity-v1"
TP_RUNTIME_REGISTERS = {
    "protocol_version": 35,
    "digest_hi": 36,
    "digest_lo": 37,
}

REASON_ORDER = (
    "RUNTIME_NOT_PROVISIONED",
    "RUNTIME_LOCK_MISMATCH",
    "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
    "CONTROL_RUNTIME_INVALID",
    "OPTIMIZER_RUNTIME_INVALID",
    "HOST_CONTRACT_MISMATCH",
    "GPU_IDENTITY_MISMATCH",
    "GPU_FUNCTIONAL_GATE_MISSING",
    "OWNER_DEPENDENCY_MISMATCH",
    "ACTIVE_SOURCE_CLOSURE_UNRESOLVED",
    "CURRENT_RELEASE_INVALID",
    "CURRENT_OBSERVATION_MISSING",
    "LAUNCH_ATTEMPT_POINTER_INVALID",
    "LAUNCH_ATTEMPT_ATTESTATION_INVALID",
    "LAUNCH_ATTEMPT_FAILED",
    "OBSERVATION_POINTER_INVALID",
    "OBSERVED_ATTESTATION_INVALID",
    "OBSERVATION_RELEASE_MISMATCH",
    "SOURCE_BINDING_MISMATCH",
    "LAUNCHER_BINDING_MISMATCH",
    "ENVIRONMENT_BINDING_MISMATCH",
    "PROCESS_TREE_BINDING_MISMATCH",
    "SAFETY_ENVELOPE_BINDING_MISMATCH",
    "OFFLINE_EVIDENCE_MISSING",
    "OFFLINE_BINDING_MISMATCH",
    "BRIDGE_PROCESS_DEAD",
    "BRIDGE_PID_REUSED",
    "BRIDGE_HEARTBEAT_STALE",
    "PROCESS_TREE_EVIDENCE_INVALID",
    "CONTROLLER_OBSERVATION_STALE",
    "CONTROLLER_FRESH_GET_BINDING_MISMATCH",
    "CONTROLLER_FRESH_GET_STALE",
    "UPLOADED_TRIPLET_MISMATCH",
    "CONTROLLER_READBACK_MISMATCH",
    "DASHBOARD_LOADED_PROGRAM_MISMATCH",
    "TP_RUNTIME_IDENTITY_UNAVAILABLE",
    "TP_RUNTIME_IDENTITY_MISMATCH",
    "RTDE_RECIPE_CAPABILITY_MISMATCH",
    "RTDE_STALE",
    "KUNWEI_STALE",
    "MULTIPLE_WRITERS",
    "MAILBOX_DIRTY",
    "MAILBOX_OBSERVATION_STALE",
    "CAMPAIGN_LEASE_MISSING",
    "CAMPAIGN_LEASE_FINGERPRINT_MISMATCH",
    "CAMPAIGN_LEASE_EXPIRED",
    "CAMPAIGN_LEASE_REVOKED",
    "CAMPAIGN_LEASE_SUPERVISOR_DEAD",
    "CAMPAIGN_LEASE_SUPERVISOR_PID_REUSED",
    "PLAY_IDENTITY_RECHECK_MISSING",
    "PLAY_IDENTITY_RECHECK_FAILED",
    "FIRST_ARM_ACK_MISSING",
    "LIVE_EVIDENCE_INVALID",
    "QUEUE_INTEGRITY_ERROR",
    "ATTEMPT_LEDGER_INTEGRITY_ERROR",
    "LEGACY_STATE_INTEGRITY_ERROR",
    "NETWORK_UNREACHABLE",
    "DASHBOARD_UNREACHABLE",
    "RTDE_UNREACHABLE",
    "KUNWEI_UNREACHABLE",
    "CONTROLLER_UNAVAILABLE",
    "UNKNOWN_OBSERVATION",
)

EXTERNAL_REASON_CODES = {
    "NETWORK_UNREACHABLE",
    "DASHBOARD_UNREACHABLE",
    "RTDE_UNREACHABLE",
    "KUNWEI_UNREACHABLE",
    "CONTROLLER_UNAVAILABLE",
    "UPLOADED_TRIPLET_MISMATCH",
    "CONTROLLER_READBACK_MISMATCH",
    "RTDE_STALE",
    "KUNWEI_STALE",
}
INTERNAL_REASON_CODES = {
    "RUNTIME_NOT_PROVISIONED",
    "RUNTIME_LOCK_MISMATCH",
    "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
    "CONTROL_RUNTIME_INVALID",
    "OPTIMIZER_RUNTIME_INVALID",
    "HOST_CONTRACT_MISMATCH",
    "GPU_IDENTITY_MISMATCH",
    "GPU_FUNCTIONAL_GATE_MISSING",
    "OWNER_DEPENDENCY_MISMATCH",
    "ACTIVE_SOURCE_CLOSURE_UNRESOLVED",
    "CURRENT_RELEASE_INVALID",
    "LAUNCH_ATTEMPT_POINTER_INVALID",
    "LAUNCH_ATTEMPT_ATTESTATION_INVALID",
    "OBSERVATION_POINTER_INVALID",
    "OBSERVED_ATTESTATION_INVALID",
    "OBSERVATION_RELEASE_MISMATCH",
    "SOURCE_BINDING_MISMATCH",
    "LAUNCHER_BINDING_MISMATCH",
    "ENVIRONMENT_BINDING_MISMATCH",
    "PROCESS_TREE_BINDING_MISMATCH",
    "SAFETY_ENVELOPE_BINDING_MISMATCH",
    "OFFLINE_EVIDENCE_MISSING",
    "OFFLINE_BINDING_MISMATCH",
    "BRIDGE_PROCESS_DEAD",
    "BRIDGE_PID_REUSED",
    "BRIDGE_HEARTBEAT_STALE",
    "PROCESS_TREE_EVIDENCE_INVALID",
    "MULTIPLE_WRITERS",
    "MAILBOX_DIRTY",
    "MAILBOX_OBSERVATION_STALE",
    "CAMPAIGN_LEASE_MISSING",
    "CAMPAIGN_LEASE_FINGERPRINT_MISMATCH",
    "CAMPAIGN_LEASE_EXPIRED",
    "CAMPAIGN_LEASE_REVOKED",
    "CAMPAIGN_LEASE_SUPERVISOR_DEAD",
    "CAMPAIGN_LEASE_SUPERVISOR_PID_REUSED",
    "PLAY_IDENTITY_RECHECK_MISSING",
    "PLAY_IDENTITY_RECHECK_FAILED",
    "QUEUE_INTEGRITY_ERROR",
    "ATTEMPT_LEDGER_INTEGRITY_ERROR",
    "LEGACY_STATE_INTEGRITY_ERROR",
}
PHYSICAL_REASON_CODES = {
    "DASHBOARD_LOADED_PROGRAM_MISMATCH",
    "TP_RUNTIME_IDENTITY_UNAVAILABLE",
    "TP_RUNTIME_IDENTITY_MISMATCH",
    "RTDE_RECIPE_CAPABILITY_MISMATCH",
}

TERMINAL_VOLATILE_REASON_CODES = {
    "BRIDGE_PROCESS_DEAD",
    "BRIDGE_PID_REUSED",
    "BRIDGE_HEARTBEAT_STALE",
    "CONTROLLER_OBSERVATION_STALE",
    "UPLOADED_TRIPLET_MISMATCH",
    "CONTROLLER_READBACK_MISMATCH",
    "DASHBOARD_LOADED_PROGRAM_MISMATCH",
    "TP_RUNTIME_IDENTITY_UNAVAILABLE",
    "TP_RUNTIME_IDENTITY_MISMATCH",
    "RTDE_STALE",
    "KUNWEI_STALE",
    "MAILBOX_DIRTY",
    "MAILBOX_OBSERVATION_STALE",
    "CAMPAIGN_LEASE_EXPIRED",
    "CAMPAIGN_LEASE_REVOKED",
    "CAMPAIGN_LEASE_SUPERVISOR_DEAD",
    "CAMPAIGN_LEASE_SUPERVISOR_PID_REUSED",
}

INVALIDATION_TABLE: Mapping[str, tuple[str, ...]] = {
    "runtime_package_changed": ("offline_proven", "play_prompt_ready", "bench_ready"),
    "runtime_lock_changed": ("offline_proven", "play_prompt_ready", "bench_ready"),
    "host_contract_changed": ("offline_proven", "play_prompt_ready", "bench_ready"),
    "gpu_identity_changed": ("offline_proven", "play_prompt_ready", "bench_ready"),
    "gpu_functional_evidence_changed": (
        "offline_proven",
        "play_prompt_ready",
        "bench_ready",
    ),
    "owner_dependency_changed": ("offline_proven", "play_prompt_ready", "bench_ready"),
    "manifest_sha_changed": (
        "release_current",
        "offline_proven",
        "lease_valid",
        "bench_ready",
    ),
    "source_fingerprint_changed": ("offline_proven", "bench_ready"),
    "launcher_changed": ("offline_proven", "bench_ready"),
    "environment_changed": ("offline_proven", "bench_ready"),
    "process_tree_changed": (
        "offline_proven",
        "bridge_process_alive",
        "bench_ready",
    ),
    "safety_envelope_changed": ("lease_valid", "bench_ready"),
    "campaign_fingerprint_changed": ("lease_valid", "bench_ready"),
    "controller_readback_changed": (
        "controller_triplet_verified",
        "bench_ready",
    ),
    "loaded_program_changed": (
        "loaded_program_verified",
        "play_identity_rechecked",
        "bench_ready",
    ),
    "tp_runtime_identity_changed": (
        "tp_runtime_identity_verified",
        "play_identity_rechecked",
        "bench_ready",
    ),
    "rtde_recipe_changed": ("rtde_recipe_capable", "bench_ready"),
    "bridge_pid_reused": (
        "bridge_process_alive",
        "bridge_heartbeat_fresh",
        "bench_ready",
    ),
    "bridge_heartbeat_stale": ("bridge_heartbeat_fresh", "bench_ready"),
    "rtde_lost": ("rtde_fresh", "bench_ready"),
    "kunwei_lost": ("kunwei_fresh", "bench_ready"),
    "writer_lock_lost": ("single_writer", "bench_ready"),
    "mailbox_dirty": ("mailbox_clean", "bench_ready"),
    "user_stop": ("lease_valid", "bench_ready"),
    "user_cancel": ("lease_valid", "bench_ready"),
    "hard_safety_fault": ("lease_valid", "bench_ready"),
    "campaign_terminal": ("lease_valid", "bench_ready"),
    "supervisor_exit": ("lease_valid", "bench_ready"),
}

TRANSITION_TABLE = (
    {"from": None, "to": "OFFLINE_PROVEN", "requires": ("offline_proven",)},
    {
        "from": "OFFLINE_PROVEN",
        "to": "WAITING_FOR_IDENTITY_PLAY",
        "requires": ("play_prompt_ready", "waiting_for_play_observed"),
    },
    {
        "from": "OFFLINE_PROVEN",
        "to": "BENCH_READY",
        "requires": ("bench_ready",),
    },
    {
        "from": "WAITING_FOR_IDENTITY_PLAY",
        "to": "BENCH_READY",
        "requires": ("bench_ready",),
    },
    {
        "from": "BENCH_READY",
        "to": "WAITING_FOR_PLAY",
        "requires": ("bench_ready", "waiting_for_play_observed"),
    },
    {
        "from": "WAITING_FOR_PLAY",
        "to": "RUNNING",
        "requires": ("bench_ready", "play_identity_rechecked", "first_arm_acknowledged"),
    },
    {
        "from": "WAITING_FOR_IDENTITY_PLAY",
        "to": "RUNNING",
        "requires": ("bench_ready", "play_identity_rechecked", "first_arm_acknowledged"),
    },
)


class GovernanceError(ValueError):
    """Observed state is malformed, stale, or not bound to the current release."""


class ObservationPointerError(GovernanceError):
    pass


class ObservedAttestationError(GovernanceError):
    pass


class LaunchAttemptPointerError(GovernanceError):
    pass


class LaunchAttemptAttestationError(GovernanceError):
    pass


@dataclass(frozen=True)
class CurrentReleaseSnapshot:
    manifest_sha256: str | None
    manifest_path: str | None
    program_id: str | None
    release_stage_id: str | None
    source_fingerprint: str | None
    launcher_sha256: str | None
    safety_envelope_sha256: str | None
    expected_triplet_sha256: Mapping[str, str] | None
    expected_loaded_program: str | None
    expected_tp_runtime_identity: Mapping[str, int] | None
    valid: bool
    error: str | None


def _strict_json_bytes(encoded: bytes, role: str) -> Any:
    def unique(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise GovernanceError(f"{role} repeats JSON key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(
            encoded.decode("utf-8", errors="strict"),
            object_pairs_hook=unique,
            parse_constant=lambda value: (_ for _ in ()).throw(
                GovernanceError(f"{role} contains non-finite {value}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GovernanceError(f"{role} is not strict JSON: {exc}") from exc


def _read_json(path: Path, role: str) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise GovernanceError(f"{role} must be a real regular file")
    payload = _strict_json_bytes(path.read_bytes(), role)
    if not isinstance(payload, Mapping):
        raise GovernanceError(f"{role} must be a JSON object")
    return payload


def _exact(value: Any, fields: set[str], role: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise GovernanceError(f"{role} fields differ")
    return value


def _text(value: Any, role: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise GovernanceError(f"{role} must be a bounded non-empty string")
    return value


def _sha256(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise GovernanceError(f"{role} must be a lowercase SHA-256")
    return value


def _positive_int(value: Any, role: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise GovernanceError(f"{role} must be a positive integer")
    return value


def _optional_positive_int(value: Any, role: str) -> int | None:
    return None if value is None else _positive_int(value, role)


def _relative_path(value: Any, role: str) -> str:
    text = _text(value, role)
    pure = PurePosixPath(text)
    if (
        pure.is_absolute()
        or pure.as_posix() != text
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise GovernanceError(f"{role} must be a normalized relative POSIX path")
    return text


def _reference(value: Any, role: str) -> dict[str, str]:
    row = _exact(value, {"path", "sha256"}, f"{role} reference")
    return {
        "path": _relative_path(row["path"], f"{role} path"),
        "sha256": _sha256(row["sha256"], f"{role} SHA-256"),
    }


def _triplet(value: Any, role: str) -> dict[str, str]:
    row = _exact(value, {".script", ".txt", ".urp"}, role)
    return {
        suffix: _sha256(row[suffix], f"{role} {suffix}")
        for suffix in (".script", ".txt", ".urp")
    }


def _optional_triplet(value: Any, role: str) -> dict[str, str] | None:
    return None if value is None else _triplet(value, role)


def _observed_tp_identity(value: Any, role: str) -> dict[str, int]:
    row = _exact(value, {"protocol_version", "digest_hi", "digest_lo"}, role)
    result: dict[str, int] = {}
    for field in ("protocol_version", "digest_hi", "digest_lo"):
        raw = row[field]
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise GovernanceError(f"{role}.{field} must be a non-negative integer")
        if field != "protocol_version" and raw >= 1 << 31:
            raise GovernanceError(f"{role}.{field} exceeds 31 bits")
        result[field] = raw
    return result


def _optional_observed_tp_identity(
    value: Any, role: str
) -> dict[str, int] | None:
    return None if value is None else _observed_tp_identity(value, role)


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("ascii")


def _file_sha256(path: Path, role: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise GovernanceError(f"{role} must be a real regular file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rooted_file(root: Path, relative: str, role: str) -> Path:
    unresolved = root / _relative_path(relative, role)
    resolved = unresolved.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise GovernanceError(f"{role} escapes its root") from exc
    if unresolved.is_symlink() or not resolved.is_file():
        raise GovernanceError(f"{role} is missing or unsafe")
    return resolved


def campaign_lease_fingerprint(
    *,
    campaign_id: str,
    manifest_sha256: str,
    campaign_fingerprint: str,
    safety_envelope_sha256: str,
    issued_at_unix_ns: int,
    expires_at_unix_ns: int,
    supervisor_pid: int,
    supervisor_starttime_ticks: int,
) -> str:
    basis = {
        "campaign_fingerprint": _sha256(
            campaign_fingerprint, "campaign fingerprint"
        ),
        "campaign_id": _text(campaign_id, "campaign ID"),
        "expires_at_unix_ns": _positive_int(expires_at_unix_ns, "lease expiry"),
        "issued_at_unix_ns": _positive_int(issued_at_unix_ns, "lease issue time"),
        "manifest_sha256": _sha256(manifest_sha256, "lease manifest SHA-256"),
        "safety_envelope_sha256": _sha256(
            safety_envelope_sha256, "lease safety-envelope SHA-256"
        ),
        "schema": CAMPAIGN_LEASE_SCHEMA,
        "supervisor_pid": _positive_int(supervisor_pid, "lease supervisor PID"),
        "supervisor_starttime_ticks": _positive_int(
            supervisor_starttime_ticks, "lease supervisor starttime"
        ),
    }
    if basis["expires_at_unix_ns"] <= basis["issued_at_unix_ns"]:
        raise GovernanceError("campaign lease expiry must follow issuance")
    return hashlib.sha256(_canonical_bytes(basis)).hexdigest()


def build_campaign_lease(
    *,
    campaign_id: str,
    manifest_sha256: str,
    campaign_fingerprint: str,
    safety_envelope_sha256: str,
    issued_at_unix_ns: int,
    expires_at_unix_ns: int,
    supervisor_pid: int,
    supervisor_starttime_ticks: int,
) -> dict[str, Any]:
    fingerprint = campaign_lease_fingerprint(
        campaign_id=campaign_id,
        manifest_sha256=manifest_sha256,
        campaign_fingerprint=campaign_fingerprint,
        safety_envelope_sha256=safety_envelope_sha256,
        issued_at_unix_ns=issued_at_unix_ns,
        expires_at_unix_ns=expires_at_unix_ns,
        supervisor_pid=supervisor_pid,
        supervisor_starttime_ticks=supervisor_starttime_ticks,
    )
    return {
        "schema": CAMPAIGN_LEASE_SCHEMA,
        "campaign_id": campaign_id,
        "manifest_sha256": manifest_sha256,
        "campaign_fingerprint": campaign_fingerprint,
        "safety_envelope_sha256": safety_envelope_sha256,
        "fingerprint": fingerprint,
        "issued_at_unix_ns": issued_at_unix_ns,
        "expires_at_unix_ns": expires_at_unix_ns,
        "supervisor_pid": supervisor_pid,
        "supervisor_starttime_ticks": supervisor_starttime_ticks,
        "revoked_at_unix_ns": None,
        "revocation_reason": None,
    }


def validate_campaign_lease(value: Any) -> dict[str, Any]:
    row = _exact(
        value,
        {
            "schema",
            "campaign_id",
            "manifest_sha256",
            "campaign_fingerprint",
            "safety_envelope_sha256",
            "fingerprint",
            "issued_at_unix_ns",
            "expires_at_unix_ns",
            "supervisor_pid",
            "supervisor_starttime_ticks",
            "revoked_at_unix_ns",
            "revocation_reason",
        },
        "campaign lease",
    )
    if row["schema"] != CAMPAIGN_LEASE_SCHEMA:
        raise GovernanceError("campaign lease schema differs")
    issued = _positive_int(row["issued_at_unix_ns"], "lease issue time")
    expires = _positive_int(row["expires_at_unix_ns"], "lease expiry")
    supervisor_pid = _positive_int(row["supervisor_pid"], "lease supervisor PID")
    supervisor_starttime = _positive_int(
        row["supervisor_starttime_ticks"], "lease supervisor starttime"
    )
    expected = campaign_lease_fingerprint(
        campaign_id=row["campaign_id"],
        manifest_sha256=row["manifest_sha256"],
        campaign_fingerprint=row["campaign_fingerprint"],
        safety_envelope_sha256=row["safety_envelope_sha256"],
        issued_at_unix_ns=issued,
        expires_at_unix_ns=expires,
        supervisor_pid=supervisor_pid,
        supervisor_starttime_ticks=supervisor_starttime,
    )
    if _sha256(row["fingerprint"], "lease fingerprint") != expected:
        raise GovernanceError("campaign lease fingerprint differs")
    revoked = _optional_positive_int(row["revoked_at_unix_ns"], "lease revocation time")
    reason = row["revocation_reason"]
    if (revoked is None) != (reason is None):
        raise GovernanceError("lease revocation time and reason must appear together")
    if reason is not None:
        _text(reason, "lease revocation reason")
        if revoked < issued:
            raise GovernanceError("lease revocation predates issuance")
    return copy.deepcopy(dict(row))


def validate_launch_attempt(value: Any) -> dict[str, Any]:
    row = _exact(
        value,
        {
            "schema",
            "sequence",
            "attempt_id",
            "state",
            "phase",
            "observed_at_unix_ns",
            "manifest_sha256",
            "exit_code",
            "reason_code",
            "detail",
            "external_evidence",
        },
        "launch attempt",
    )
    if row["schema"] != LAUNCH_ATTEMPT_SCHEMA:
        raise GovernanceError("launch attempt schema differs")
    _positive_int(row["sequence"], "launch attempt sequence")
    _text(row["attempt_id"], "launch attempt ID")
    state = row["state"]
    if state not in LAUNCH_ATTEMPT_STATES:
        raise GovernanceError("launch attempt state differs")
    phase = row["phase"]
    if phase not in LAUNCH_ATTEMPT_PHASES:
        raise GovernanceError("launch attempt phase differs")
    _positive_int(row["observed_at_unix_ns"], "launch attempt observation")
    manifest_sha = row["manifest_sha256"]
    if manifest_sha is not None:
        _sha256(manifest_sha, "launch attempt manifest SHA-256")
    detail = row["detail"]
    if detail is not None:
        _text(detail, "launch attempt detail")

    if state == "STARTED":
        if any(
            row[name] is not None
            for name in ("exit_code", "reason_code", "detail", "external_evidence")
        ):
            raise GovernanceError("started launch attempt contains failure fields")
    else:
        exit_code = _positive_int(row["exit_code"], "launch attempt exit code")
        if exit_code > 255:
            raise GovernanceError("launch attempt exit code exceeds shell status range")
        reason = _text(row["reason_code"], "launch attempt reason code")
        if reason != "LAUNCH_ATTEMPT_FAILED" and reason not in EXTERNAL_REASON_CODES:
            raise GovernanceError("launch attempt reason code differs")
        external = row["external_evidence"]
        if reason in EXTERNAL_REASON_CODES:
            if external is None:
                raise GovernanceError(
                    "external launch blocker requires positive evidence"
                )
            _reference(external, "launch attempt external evidence")
        elif external is not None:
            raise GovernanceError(
                "generic launch failure cannot claim external evidence"
            )
    return copy.deepcopy(dict(row))


def _validate_event_timestamp(value: Any, role: str, observed_at: int) -> int | None:
    timestamp = _optional_positive_int(value, role)
    if timestamp is not None and timestamp > observed_at:
        raise GovernanceError(f"{role} follows the attestation observation")
    return timestamp


def validate_observed_attestation(value: Any) -> dict[str, Any]:
    row = _exact(
        value,
        {
            "schema",
            "sequence",
            "run_id",
            "campaign_id",
            "observed_at_unix_ns",
            "bindings",
            "offline",
            "process",
            "controller",
            "mailbox",
            "lease",
            "events",
            "external_blocker",
        },
        "observed attestation",
    )
    if row["schema"] != OBSERVED_ATTESTATION_SCHEMA:
        raise GovernanceError("observed attestation schema differs")
    _positive_int(row["sequence"], "attestation sequence")
    _text(row["run_id"], "run ID")
    campaign_id = _text(row["campaign_id"], "campaign ID")
    observed_at = _positive_int(row["observed_at_unix_ns"], "observation time")

    bindings = _exact(
        row["bindings"],
        {
            "manifest_sha256",
            "source_fingerprint",
            "launcher_sha256",
            "environment_sha256",
            "process_tree_fingerprint",
            "safety_envelope_sha256",
            "campaign_fingerprint",
        },
        "attestation bindings",
    )
    for field in bindings:
        _sha256(bindings[field], f"attestation binding {field}")

    offline = _exact(
        row["offline"],
        {
            "evidence",
            "completed_at_unix_ns",
            "manifest_sha256",
            "source_fingerprint",
            "launcher_sha256",
            "environment_sha256",
            "process_tree_fingerprint",
        },
        "offline qualification",
    )
    _reference(offline["evidence"], "offline qualification")
    _validate_event_timestamp(
        offline["completed_at_unix_ns"], "offline completion", observed_at
    )
    for field in (
        "manifest_sha256",
        "source_fingerprint",
        "launcher_sha256",
        "environment_sha256",
        "process_tree_fingerprint",
    ):
        _sha256(offline[field], f"offline {field}")

    process = _exact(
        row["process"],
        {
            "bridge_pid",
            "bridge_starttime_ticks",
            "heartbeat_at_unix_ns",
            "process_tree_fingerprint",
            "writer_pids",
            "evidence",
        },
        "bridge process observation",
    )
    _positive_int(process["bridge_pid"], "bridge PID")
    _positive_int(process["bridge_starttime_ticks"], "bridge starttime")
    _validate_event_timestamp(
        process["heartbeat_at_unix_ns"], "bridge heartbeat", observed_at
    )
    _sha256(process["process_tree_fingerprint"], "process-tree fingerprint")
    writers = process["writer_pids"]
    if (
        not isinstance(writers, list)
        or any(
            isinstance(pid, bool) or not isinstance(pid, int) or pid < 1
            for pid in writers
        )
        or writers != sorted(set(writers))
    ):
        raise GovernanceError("writer_pids must be sorted unique positive PIDs")
    _reference(process["evidence"], "process tree")

    controller = _exact(
        row["controller"],
        {
            "observed_at_unix_ns",
            "fresh_get_observed_at_unix_ns",
            "delivery_transaction_id",
            "uploaded_triplet_sha256",
            "readback_triplet_sha256",
            "loaded_program",
            "tp_runtime_identity",
            "rtde_output_fields",
            "rtde_output_types",
            "rtde_observed_at_unix_ns",
            "kunwei_observed_at_unix_ns",
            "delivery_observation",
            "evidence",
        },
        "controller observation",
    )
    _validate_event_timestamp(
        controller["observed_at_unix_ns"], "controller observation", observed_at
    )
    _validate_event_timestamp(
        controller["fresh_get_observed_at_unix_ns"],
        "controller fresh-GET observation",
        observed_at,
    )
    delivery_transaction_id = _text(
        controller["delivery_transaction_id"], "controller delivery transaction ID"
    )
    if len(delivery_transaction_id) != 32 or any(
        character not in "0123456789abcdef"
        for character in delivery_transaction_id
    ):
        raise GovernanceError("controller delivery transaction ID is invalid")
    _optional_triplet(controller["uploaded_triplet_sha256"], "uploaded triplet")
    _optional_triplet(controller["readback_triplet_sha256"], "readback triplet")
    if controller["loaded_program"] is not None:
        _text(controller["loaded_program"], "loaded program")
    _optional_observed_tp_identity(
        controller["tp_runtime_identity"], "controller TP runtime identity"
    )
    rtde_fields = controller["rtde_output_fields"]
    rtde_types = controller["rtde_output_types"]
    if (
        not isinstance(rtde_fields, list)
        or not isinstance(rtde_types, list)
        or any(not isinstance(value, str) or not value for value in rtde_fields)
        or any(not isinstance(value, str) or not value for value in rtde_types)
    ):
        raise GovernanceError("RTDE output recipe fields and types must be string lists")
    _validate_event_timestamp(
        controller["rtde_observed_at_unix_ns"], "RTDE observation", observed_at
    )
    _validate_event_timestamp(
        controller["kunwei_observed_at_unix_ns"], "Kunwei observation", observed_at
    )
    _reference(controller["delivery_observation"], "delivery observation")
    _reference(controller["evidence"], "controller observation")

    mailbox = _exact(
        row["mailbox"],
        {
            "observed_at_unix_ns",
            "pending_arm_sequence",
            "duplicate_arm_detected",
            "evidence",
        },
        "mailbox observation",
    )
    _validate_event_timestamp(
        mailbox["observed_at_unix_ns"], "mailbox observation", observed_at
    )
    _optional_positive_int(mailbox["pending_arm_sequence"], "pending ARM sequence")
    if not isinstance(mailbox["duplicate_arm_detected"], bool):
        raise GovernanceError("duplicate_arm_detected must be boolean")
    _reference(mailbox["evidence"], "mailbox observation")

    lease = None if row["lease"] is None else validate_campaign_lease(row["lease"])
    if lease is not None and lease["campaign_id"] != campaign_id:
        raise GovernanceError("campaign lease campaign differs from attestation")
    if lease is not None and lease["issued_at_unix_ns"] > observed_at:
        raise GovernanceError("campaign lease issuance follows the attestation")
    if (
        lease is not None
        and lease["revoked_at_unix_ns"] is not None
        and lease["revoked_at_unix_ns"] > observed_at
    ):
        raise GovernanceError("campaign lease revocation follows the attestation")

    events = _exact(
        row["events"],
        {
            "waiting_for_play_at_unix_ns",
            "play_observed_at_unix_ns",
            "play_identity_recheck",
            "first_arm_ack",
            "trial_completion",
            "next_arm_ack",
            "campaign_terminal",
        },
        "lifecycle events",
    )
    waiting = _validate_event_timestamp(
        events["waiting_for_play_at_unix_ns"], "waiting-for-Play event", observed_at
    )
    play = _validate_event_timestamp(
        events["play_observed_at_unix_ns"], "Play observation", observed_at
    )
    recheck = events["play_identity_recheck"]
    recheck_at: int | None = None
    if recheck is not None:
        recheck = _exact(
            recheck,
            {
                "observed_at_unix_ns",
                "readback_triplet_sha256",
                "loaded_program",
                "tp_runtime_identity",
            },
            "Play identity recheck",
        )
        recheck_at = _validate_event_timestamp(
            recheck["observed_at_unix_ns"], "Play identity recheck", observed_at
        )
        _triplet(recheck["readback_triplet_sha256"], "Play recheck triplet")
        _text(recheck["loaded_program"], "Play recheck loaded program")
        _observed_tp_identity(recheck["tp_runtime_identity"], "Play recheck TP identity")
    first_ack = events["first_arm_ack"]
    first_ack_at: int | None = None
    first_sequence: int | None = None
    if first_ack is not None:
        first_ack = _exact(
            first_ack, {"sequence", "observed_at_unix_ns"}, "first ARM acknowledgement"
        )
        first_sequence = _positive_int(first_ack["sequence"], "first ARM sequence")
        first_ack_at = _validate_event_timestamp(
            first_ack["observed_at_unix_ns"], "first ARM acknowledgement", observed_at
        )
    trial = events["trial_completion"]
    trial_at: int | None = None
    if trial is not None:
        trial = _exact(
            trial,
            {"trial_id", "observed_at_unix_ns", "evidence"},
            "trial completion",
        )
        _text(trial["trial_id"], "trial ID")
        trial_at = _validate_event_timestamp(
            trial["observed_at_unix_ns"], "trial completion", observed_at
        )
        _reference(trial["evidence"], "trial completion")
    next_ack = events["next_arm_ack"]
    next_ack_at: int | None = None
    next_sequence: int | None = None
    if next_ack is not None:
        next_ack = _exact(
            next_ack, {"sequence", "observed_at_unix_ns"}, "next ARM acknowledgement"
        )
        next_sequence = _positive_int(next_ack["sequence"], "next ARM sequence")
        next_ack_at = _validate_event_timestamp(
            next_ack["observed_at_unix_ns"], "next ARM acknowledgement", observed_at
        )
    terminal = events["campaign_terminal"]
    terminal_at: int | None = None
    if terminal is not None:
        terminal = _exact(
            terminal,
            {"observed_at_unix_ns", "reason", "runner_exit_code"},
            "campaign terminal event",
        )
        terminal_at = _validate_event_timestamp(
            terminal["observed_at_unix_ns"], "campaign terminal event", observed_at
        )
        if terminal["reason"] != "campaign_complete":
            raise GovernanceError("campaign terminal reason differs")
        runner_exit_code = terminal["runner_exit_code"]
        if (
            isinstance(runner_exit_code, bool)
            or not isinstance(runner_exit_code, int)
            or runner_exit_code != 0
        ):
            raise GovernanceError("campaign terminal requires runner exit code zero")

    if play is not None and (waiting is None or play < waiting):
        raise GovernanceError("Play observation requires a preceding waiting-for-Play event")
    if recheck_at is not None and (play is None or recheck_at < play):
        raise GovernanceError("Play identity recheck must follow Play")
    if first_ack_at is not None and (recheck_at is None or first_ack_at < recheck_at):
        raise GovernanceError("first ARM acknowledgement must follow identity recheck")
    if trial_at is not None and (first_ack_at is None or trial_at < first_ack_at):
        raise GovernanceError("trial completion must follow first ARM acknowledgement")
    if next_ack_at is not None and (
        trial_at is None
        or next_ack_at < trial_at
        or first_sequence is None
        or next_sequence is None
        or next_sequence <= first_sequence
    ):
        raise GovernanceError("next ARM acknowledgement must follow the completed trial")
    if terminal_at is not None:
        if next_ack_at is None or terminal_at < next_ack_at:
            raise GovernanceError(
                "campaign terminal event must follow the next ARM acknowledgement"
            )
        if (
            lease is None
            or lease["revoked_at_unix_ns"] is None
            or lease["revocation_reason"] != "campaign_terminal"
            or terminal_at < lease["revoked_at_unix_ns"]
        ):
            raise GovernanceError(
                "campaign terminal event requires a preceding terminal lease revocation"
            )

    external = row["external_blocker"]
    if external is not None:
        external = _exact(
            external,
            {"reason_code", "observed_at_unix_ns", "evidence"},
            "external blocker",
        )
        if external["reason_code"] not in EXTERNAL_REASON_CODES:
            raise GovernanceError("external blocker reason code differs")
        _validate_event_timestamp(
            external["observed_at_unix_ns"], "external blocker observation", observed_at
        )
        _reference(external["evidence"], "external blocker")
    return copy.deepcopy(dict(row))


def read_proc_starttime_ticks(pid: int) -> int | None:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
        return None
    try:
        encoded = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        closing = encoded.rfind(")")
        if closing < 0:
            return None
        fields = encoded[closing + 2 :].split()
        return int(fields[19])
    except (OSError, UnicodeError, ValueError, IndexError):
        return None


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_bytes(path: Path, encoded: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.parent.is_symlink():
        raise GovernanceError(f"refusing unsafe observation path: {path}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    finally:
        temporary_path.unlink(missing_ok=True)


def _campaign_root(path: Path) -> Path:
    root = path.expanduser().absolute()
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise GovernanceError("campaign root must be a real directory")
    return root


def _ensure_directory(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise GovernanceError(f"observation directory is unsafe: {path}")
        return
    path.mkdir(mode=0o700)
    _fsync_directory(path.parent)


def _validate_current_launch_attempt_pointer(value: Any) -> dict[str, Any]:
    row = _exact(
        value,
        {
            "schema",
            "sequence",
            "attempt_id",
            "attestation_path",
            "attestation_sha256",
        },
        "current launch-attempt pointer",
    )
    if row["schema"] != CURRENT_LAUNCH_ATTEMPT_POINTER_SCHEMA:
        raise GovernanceError("current launch-attempt pointer schema differs")
    sequence = _positive_int(row["sequence"], "current launch-attempt sequence")
    attempt_id = _text(row["attempt_id"], "current launch-attempt ID")
    attestation_sha = _sha256(
        row["attestation_sha256"], "launch-attempt attestation SHA-256"
    )
    relative = _relative_path(
        row["attestation_path"], "launch-attempt attestation path"
    )
    expected = (
        f"governance/launch-attempts/{attestation_sha}/attestation.json"
    )
    if relative != expected:
        raise GovernanceError("current launch-attempt path is not content-addressed")
    return {
        "schema": CURRENT_LAUNCH_ATTEMPT_POINTER_SCHEMA,
        "sequence": sequence,
        "attempt_id": attempt_id,
        "attestation_path": relative,
        "attestation_sha256": attestation_sha,
    }


def load_current_launch_attempt(
    campaign_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    root = _campaign_root(campaign_root)
    pointer_path = root / "governance/current-launch.json"
    try:
        pointer = _validate_current_launch_attempt_pointer(
            _read_json(pointer_path, "current launch-attempt pointer")
        )
    except GovernanceError as exc:
        raise LaunchAttemptPointerError(str(exc)) from exc
    try:
        attestation_path = _rooted_file(
            root,
            pointer["attestation_path"],
            "current launch-attempt attestation path",
        )
        encoded = attestation_path.read_bytes()
        if hashlib.sha256(encoded).hexdigest() != pointer["attestation_sha256"]:
            raise GovernanceError("current launch-attempt attestation digest differs")
        attestation = validate_launch_attempt(
            _strict_json_bytes(encoded, "current launch-attempt attestation")
        )
        if attestation["sequence"] != pointer["sequence"]:
            raise GovernanceError("current launch-attempt sequence differs")
        if attestation["attempt_id"] != pointer["attempt_id"]:
            raise GovernanceError("current launch-attempt ID differs")
    except GovernanceError as exc:
        raise LaunchAttemptAttestationError(str(exc)) from exc
    return attestation, pointer


def publish_launch_attempt(
    campaign_root: Path,
    *,
    attempt_id: str,
    state: str,
    phase: str,
    manifest_sha256: str | None = None,
    observed_at_unix_ns: int | None = None,
    exit_code: int | None = None,
    reason_code: str | None = None,
    detail: str | None = None,
    external_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root = _campaign_root(campaign_root)
    current: Mapping[str, Any] | None = None
    pointer_path = root / "governance/current-launch.json"
    if pointer_path.exists():
        current, current_pointer = load_current_launch_attempt(root)
        sequence = current_pointer["sequence"] + 1
    else:
        sequence = 1

    payload = validate_launch_attempt(
        {
            "schema": LAUNCH_ATTEMPT_SCHEMA,
            "sequence": sequence,
            "attempt_id": attempt_id,
            "state": state,
            "phase": phase,
            "observed_at_unix_ns": (
                time.time_ns()
                if observed_at_unix_ns is None
                else observed_at_unix_ns
            ),
            "manifest_sha256": manifest_sha256,
            "exit_code": exit_code,
            "reason_code": reason_code,
            "detail": detail,
            "external_evidence": (
                None if external_evidence is None else dict(external_evidence)
            ),
        }
    )
    if current is not None:
        if payload["observed_at_unix_ns"] < current["observed_at_unix_ns"]:
            raise GovernanceError("launch attempt observation cannot move backwards")
        same_attempt = current["attempt_id"] == payload["attempt_id"]
        if not same_attempt and payload["state"] != "STARTED":
            raise GovernanceError("a new launch attempt must start before failing")
        if same_attempt:
            if current["state"] != "STARTED":
                raise GovernanceError("a failed launch attempt is terminal")
            current_phase = LAUNCH_ATTEMPT_PHASES.index(current["phase"])
            next_phase = LAUNCH_ATTEMPT_PHASES.index(payload["phase"])
            if next_phase < current_phase:
                raise GovernanceError("launch attempt phase cannot move backwards")
            if payload["state"] == "FAILED" and next_phase != current_phase:
                raise GovernanceError("launch attempt must fail in its active phase")

    if payload["external_evidence"] is not None and not _reference_is_current(
        root, payload["external_evidence"]
    ):
        raise GovernanceError("launch attempt external evidence is unavailable")

    encoded = _canonical_bytes(payload)
    digest = hashlib.sha256(encoded).hexdigest()
    relative = f"governance/launch-attempts/{digest}/attestation.json"
    destination = root / relative
    _ensure_directory(root)
    _ensure_directory(root / "governance")
    _ensure_directory(root / "governance/launch-attempts")
    _ensure_directory(destination.parent)
    if destination.exists():
        if destination.is_symlink() or destination.read_bytes() != encoded:
            raise GovernanceError("content-addressed launch-attempt bytes differ")
    else:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(destination.parent)
    pointer = {
        "schema": CURRENT_LAUNCH_ATTEMPT_POINTER_SCHEMA,
        "sequence": payload["sequence"],
        "attempt_id": payload["attempt_id"],
        "attestation_path": relative,
        "attestation_sha256": digest,
    }
    _atomic_bytes(pointer_path, _canonical_bytes(pointer))
    return {"attestation": payload, "pointer": pointer}


def publish_observed_attestation(
    campaign_root: Path, payload: Mapping[str, Any]
) -> dict[str, Any]:
    root = _campaign_root(campaign_root)
    attestation = validate_observed_attestation(payload)
    encoded = _canonical_bytes(attestation)
    digest = hashlib.sha256(encoded).hexdigest()
    relative = f"governance/attestations/{digest}/attestation.json"
    destination = root / relative
    _ensure_directory(root)
    _ensure_directory(root / "governance")
    _ensure_directory(root / "governance/attestations")
    _ensure_directory(destination.parent)
    if destination.exists():
        if destination.is_symlink() or destination.read_bytes() != encoded:
            raise GovernanceError("content-addressed attestation bytes differ")
    else:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(destination.parent)

    pointer_path = root / "governance/current.json"
    pointer = {
        "schema": CURRENT_OBSERVATION_POINTER_SCHEMA,
        "sequence": attestation["sequence"],
        "manifest_sha256": attestation["bindings"]["manifest_sha256"],
        "attestation_path": relative,
        "attestation_sha256": digest,
    }
    if pointer_path.exists():
        current = _read_json(pointer_path, "current observation pointer")
        _validate_current_pointer(current)
        if current["sequence"] > pointer["sequence"]:
            raise GovernanceError("current observation sequence cannot move backwards")
        if current["sequence"] == pointer["sequence"]:
            if current != pointer:
                raise GovernanceError("current observation sequence is not idempotent")
            return dict(current)
    _atomic_bytes(pointer_path, _canonical_bytes(pointer))
    return pointer


def _validate_current_pointer(value: Any) -> dict[str, Any]:
    row = _exact(
        value,
        {
            "schema",
            "sequence",
            "manifest_sha256",
            "attestation_path",
            "attestation_sha256",
        },
        "current observation pointer",
    )
    if row["schema"] != CURRENT_OBSERVATION_POINTER_SCHEMA:
        raise GovernanceError("current observation pointer schema differs")
    sequence = _positive_int(row["sequence"], "current observation sequence")
    manifest_sha = _sha256(row["manifest_sha256"], "pointer manifest SHA-256")
    attestation_sha = _sha256(
        row["attestation_sha256"], "pointer attestation SHA-256"
    )
    relative = _relative_path(row["attestation_path"], "pointer attestation path")
    expected = f"governance/attestations/{attestation_sha}/attestation.json"
    if relative != expected:
        raise GovernanceError("current observation path is not content-addressed")
    return {
        "schema": CURRENT_OBSERVATION_POINTER_SCHEMA,
        "sequence": sequence,
        "manifest_sha256": manifest_sha,
        "attestation_path": relative,
        "attestation_sha256": attestation_sha,
    }


def load_current_observation(campaign_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    root = _campaign_root(campaign_root)
    pointer_path = root / "governance/current.json"
    try:
        pointer = _validate_current_pointer(
            _read_json(pointer_path, "current observation pointer")
        )
    except GovernanceError as exc:
        raise ObservationPointerError(str(exc)) from exc
    try:
        attestation_path = _rooted_file(
            root, pointer["attestation_path"], "current attestation path"
        )
        encoded = attestation_path.read_bytes()
        if hashlib.sha256(encoded).hexdigest() != pointer["attestation_sha256"]:
            raise GovernanceError("current attestation digest differs")
        attestation = validate_observed_attestation(
            _strict_json_bytes(encoded, "current observed attestation")
        )
        if attestation["sequence"] != pointer["sequence"]:
            raise GovernanceError("current observation sequence differs")
        if attestation["bindings"]["manifest_sha256"] != pointer["manifest_sha256"]:
            raise GovernanceError("current observation manifest binding differs")
    except GovernanceError as exc:
        raise ObservedAttestationError(str(exc)) from exc
    return attestation, pointer


def _actual_source_fingerprint(root: Path, manifest: Mapping[str, Any]) -> str:
    sources = manifest.get("source_fingerprints")
    verification = manifest.get("verification")
    if not isinstance(sources, Mapping) or not isinstance(verification, Mapping):
        raise GovernanceError("release source fingerprint maps are missing")
    repository_sources = verification.get("repository_source_fingerprints")
    depth = verification.get("repository_source_root_depth")
    if (
        not isinstance(repository_sources, Mapping)
        or isinstance(depth, bool)
        or not isinstance(depth, int)
        or not 0 <= depth <= 4
    ):
        raise GovernanceError("release repository source coverage differs")
    actual: dict[str, str] = {}
    for relative in sorted(sources):
        path = _rooted_file(root, relative, f"release source {relative}")
        actual[f"experiment:{relative}"] = _file_sha256(path, f"release source {relative}")
    repository_root = root
    for _ in range(depth):
        repository_root = repository_root.parent
    repository_root = repository_root.resolve(strict=True)
    for relative in sorted(repository_sources):
        path = _rooted_file(
            repository_root, relative, f"repository source {relative}"
        )
        actual[f"repository:{relative}"] = _file_sha256(
            path, f"repository source {relative}"
        )
    return hashlib.sha256(_canonical_bytes(actual)).hexdigest()


def load_current_release_snapshot(experiment_root: Path) -> CurrentReleaseSnapshot:
    root = experiment_root.expanduser().resolve(strict=True)
    values: dict[str, Any] = {
        "manifest_sha256": None,
        "manifest_path": None,
        "program_id": None,
        "release_stage_id": None,
        "source_fingerprint": None,
        "launcher_sha256": None,
        "safety_envelope_sha256": None,
        "expected_triplet_sha256": None,
        "expected_loaded_program": None,
        "expected_tp_runtime_identity": None,
        "valid": False,
        "error": None,
    }
    try:
        pointer = _exact(
            _read_json(root / "config/step5d/current.json", "current release pointer"),
            {"schema", "manifest_path", "manifest_sha256"},
            "current release pointer",
        )
        if pointer["schema"] != "step5d.autotune-v3/current-release-pointer-v1":
            raise GovernanceError("current release pointer schema differs")
        manifest_sha = _sha256(
            pointer["manifest_sha256"], "current manifest SHA-256"
        )
        manifest_relative = _relative_path(
            pointer["manifest_path"], "current manifest path"
        )
        values["manifest_sha256"] = manifest_sha
        values["manifest_path"] = manifest_relative
        manifest_path = _rooted_file(root, manifest_relative, "current manifest")
        encoded = manifest_path.read_bytes()
        if hashlib.sha256(encoded).hexdigest() != manifest_sha:
            raise GovernanceError("current manifest SHA-256 differs")
        if (
            manifest_path.parent.name != manifest_sha
            or manifest_path.name != "manifest.json"
        ):
            raise GovernanceError("current manifest is not content-addressed")
        manifest = _strict_json_bytes(encoded, "current release manifest")
        if not isinstance(manifest, Mapping):
            raise GovernanceError("current release manifest must be an object")
        identity = manifest.get("identity")
        if not isinstance(identity, Mapping):
            raise GovernanceError("current release identity is missing")
        values["program_id"] = _text(identity.get("program_id"), "release program ID")
        values["release_stage_id"] = _text(
            identity.get("release_stage_id"), "release stage ID"
        )

        artifacts = _exact(
            manifest.get("artifacts"), {".script", ".txt", ".urp"}, "release artifacts"
        )
        expected_triplet: dict[str, str] = {}
        for suffix, reference in artifacts.items():
            reference = _exact(reference, {"path", "sha256"}, f"artifact {suffix}")
            expected_triplet[suffix] = _sha256(
                reference["sha256"], f"artifact {suffix} SHA-256"
            )
        values["expected_triplet_sha256"] = expected_triplet

        tp = _exact(
            manifest.get("tp_runtime_identity"),
            TP_RUNTIME_IDENTITY_FIELDS,
            "TP runtime identity",
        )
        if tp["schema"] != TP_RUNTIME_IDENTITY_SCHEMA:
            raise GovernanceError("TP runtime identity schema differs")
        if tp["program_id"] != values["program_id"] or tp["protocol_id"] != identity.get(
            "protocol_id"
        ):
            raise GovernanceError("TP runtime identity release binding differs")
        if tp["registers"] != TP_RUNTIME_REGISTERS:
            raise GovernanceError("TP runtime identity register map differs")
        if tp["script_artifact_sha256"] != expected_triplet[".script"]:
            raise GovernanceError("TP runtime identity script artifact differs")
        _sha256(tp["script_basis_sha256"], "TP script basis SHA-256")
        values["expected_tp_runtime_identity"] = _observed_tp_identity(
            {
                "protocol_version": tp["protocol_version"],
                "digest_hi": tp["digest_hi"],
                "digest_lo": tp["digest_lo"],
            },
            "manifest TP runtime identity",
        )

        safety = _reference(manifest.get("safety_envelope"), "safety envelope")
        safety_unresolved = manifest_path.parent / _relative_path(
            safety["path"], "safety envelope path"
        )
        safety_path = safety_unresolved.resolve()
        try:
            safety_path.relative_to(manifest_path.parent)
        except ValueError as exc:
            raise GovernanceError(
                "safety envelope escapes immutable release"
            ) from exc
        if safety_unresolved.is_symlink() or not safety_path.is_file():
            raise GovernanceError("safety envelope is missing or unsafe")
        if _file_sha256(safety_path, "safety envelope") != safety["sha256"]:
            raise GovernanceError("safety-envelope SHA-256 differs")
        values["safety_envelope_sha256"] = safety["sha256"]

        values["expected_loaded_program"] = _text(
            manifest.get("controller_target"), "controller target"
        )

        launcher = root / "scripts/step5d-autotune-v3.sh"
        values["launcher_sha256"] = _file_sha256(launcher, "canonical launcher")
        values["source_fingerprint"] = _actual_source_fingerprint(root, manifest)

        from .release_identity import load_current_release

        load_current_release(root)
        values["valid"] = True
    except Exception as exc:
        values["error"] = f"{type(exc).__name__}:{exc}"
    return CurrentReleaseSnapshot(**values)


def _age_fresh(now_ns: int, observed_ns: Any, limit_ns: int) -> bool:
    return (
        isinstance(observed_ns, int)
        and not isinstance(observed_ns, bool)
        and 0 <= now_ns - observed_ns <= limit_ns
    )


def _reference_is_current(campaign_root: Path, value: Any) -> bool:
    try:
        reference = _reference(value, "evidence")
        path = _rooted_file(campaign_root, reference["path"], "evidence")
        return _file_sha256(path, "evidence") == reference["sha256"]
    except GovernanceError:
        return False


def _delivery_provenance_is_current(
    campaign_root: Path,
    value: Any,
    *,
    transaction_id: str,
    observed_at_unix_ns: int,
) -> bool:
    try:
        reference = _reference(value, "delivery observation evidence")
        path = _rooted_file(
            campaign_root, reference["path"], "delivery observation evidence"
        )
        if _file_sha256(path, "delivery observation evidence") != reference["sha256"]:
            return False
        payload = _strict_json_bytes(path.read_bytes(), "delivery observation evidence")
        if not isinstance(payload, Mapping):
            return False
        provenance = fresh_get_provenance(payload)
        return provenance == {
            "transaction_id": transaction_id,
            "observed_at_unix_ns": observed_at_unix_ns,
        }
    except (DeliveryObservationError, GovernanceError):
        return False


def _load_current_offline_proof(
    experiment_root: Path,
    campaign_root: Path,
    release: CurrentReleaseSnapshot,
) -> Mapping[str, Any] | None:
    if not release.valid:
        return None
    root = _campaign_root(campaign_root)
    pointer_path = root / "qualification/current.json"
    if not pointer_path.exists():
        return None
    pointer = _exact(
        _read_json(pointer_path, "qualification current pointer"),
        {"schema", "cache_key", "path", "sha256"},
        "qualification current pointer",
    )
    if pointer["schema"] != QUALIFICATION_CURRENT_POINTER_SCHEMA:
        raise GovernanceError("qualification current pointer schema differs")
    _sha256(pointer["cache_key"], "qualification cache key")
    reference = {
        "path": _relative_path(pointer["path"], "qualification evidence path"),
        "sha256": _sha256(pointer["sha256"], "qualification evidence SHA-256"),
    }
    evidence_path = _rooted_file(root, reference["path"], "qualification evidence")
    if _file_sha256(evidence_path, "qualification evidence") != reference["sha256"]:
        raise GovernanceError("qualification evidence SHA-256 differs")
    payload = _read_json(evidence_path, "qualification evidence")
    try:
        from .qualification import validate_qualification_result

        binding = validate_qualification_result(
            payload,
            experiment_root=experiment_root,
            manifest_sha256=release.manifest_sha256,
            source_fingerprint=release.source_fingerprint,
            launcher_sha256=release.launcher_sha256,
        )
    except Exception as exc:
        raise GovernanceError(f"qualification evidence is invalid: {exc}") from exc
    return {
        "evidence": reference,
        "completed_at_unix_ns": _positive_int(
            payload.get("completed_at_unix_ns"), "qualification completion"
        ),
        "manifest_sha256": release.manifest_sha256,
        "source_fingerprint": release.source_fingerprint,
        "launcher_sha256": release.launcher_sha256,
        "environment_sha256": _sha256(
            binding["environment"]["fingerprint"], "qualification environment"
        ),
        "process_tree_fingerprint": _sha256(
            binding["process_tree"]["fingerprint"], "qualification process tree"
        ),
    }


def _evidence_row(
    role: str,
    reference: Mapping[str, Any] | None = None,
    *,
    detail: str | None = None,
) -> dict[str, Any]:
    return {
        "role": role,
        "path": None if reference is None else reference.get("path"),
        "sha256": None if reference is None else reference.get("sha256"),
        "detail": detail,
    }


def _ordered_reasons(reasons: Sequence[str]) -> list[str]:
    unique = set(reasons)
    ordered = [reason for reason in REASON_ORDER if reason in unique]
    ordered.extend(sorted(unique - set(REASON_ORDER)))
    return ordered


def _blocker_class(reasons: Sequence[str]) -> str | None:
    values = set(reasons)
    if values & INTERNAL_REASON_CODES:
        return "INTERNAL"
    if values & EXTERNAL_REASON_CODES:
        return "BLOCKED_EXTERNAL"
    if values & PHYSICAL_REASON_CODES:
        return "PHYSICAL"
    return "UNKNOWN" if values else None


def _next_action(state: str | None, reasons: Sequence[str], live_proven: bool) -> str:
    values = set(reasons)
    environment_actions = {
        "RUNTIME_NOT_PROVISIONED": "provision_runtime",
        "RUNTIME_LOCK_MISMATCH": "provision_runtime_for_current_lock",
        "RUNTIME_PACKAGE_INTEGRITY_MISMATCH": "reprovision_runtime",
        "CONTROL_RUNTIME_INVALID": "reprovision_control_runtime",
        "OPTIMIZER_RUNTIME_INVALID": "reprovision_optimizer_runtime",
        "HOST_CONTRACT_MISMATCH": "restore_host_contract",
        "GPU_IDENTITY_MISMATCH": "restore_governed_gpu_identity",
        "GPU_FUNCTIONAL_GATE_MISSING": "run_native_gpu_functional_gates",
        "OWNER_DEPENDENCY_MISMATCH": "restore_owner_dependency",
        "ACTIVE_SOURCE_CLOSURE_UNRESOLVED": "resolve_active_source_closure",
    }
    for reason in REASON_ORDER:
        if reason in values and reason in environment_actions:
            return environment_actions[reason]
    if "CURRENT_RELEASE_INVALID" in values:
        return "repair_current_release"
    if "CURRENT_OBSERVATION_MISSING" in values:
        return "start_canonical_bridge"
    blocker_class = _blocker_class(reasons)
    if blocker_class == "INTERNAL":
        return "repair_internal_governance_state"
    if blocker_class == "PHYSICAL":
        return "correct_physical_controller_state"
    if blocker_class == "BLOCKED_EXTERNAL":
        return "restore_external_dependency"
    if "FIRST_ARM_ACK_MISSING" in values:
        return "wait_for_first_arm_ack"
    if blocker_class == "UNKNOWN":
        return "collect_read_only_evidence"
    if state == "OFFLINE_PROVEN":
        return "start_canonical_bridge"
    if state == "BENCH_READY":
        return "publish_waiting_for_play"
    if state in {"WAITING_FOR_IDENTITY_PLAY", "WAITING_FOR_PLAY"}:
        return "press_play_or_stop"
    if state == "RUNNING" and not live_proven:
        return "monitor_until_next_arm_ack"
    if state == "RUNNING":
        return "continue_or_stop_campaign"
    return "run_production_qualification"


def _empty_predicates() -> dict[str, bool]:
    return {
        "release_current": False,
        "offline_proven": False,
        "controller_fresh": False,
        "controller_fresh_get": False,
        "uploaded_identity_verified": False,
        "controller_triplet_verified": False,
        "loaded_program_verified": False,
        "tp_runtime_identity_verified": False,
        "rtde_recipe_capable": False,
        "rtde_fresh": False,
        "kunwei_fresh": False,
        "bridge_process_alive": False,
        "bridge_heartbeat_fresh": False,
        "single_writer": False,
        "mailbox_clean": False,
        "lease_valid": False,
        "play_identity_rechecked": False,
        "play_prompt_ready": False,
        "bench_ready": False,
        "first_arm_acknowledged": False,
    }


def reduce_observed_attestation(
    release: CurrentReleaseSnapshot,
    attestation: Mapping[str, Any] | None,
    *,
    campaign_root: Path,
    now_ns: int,
    proc_starttime_reader: Callable[[int], int | None] = read_proc_starttime_ticks,
    pointer: Mapping[str, Any] | None = None,
    initial_reasons: Sequence[str] = (),
    initial_evidence: Sequence[Mapping[str, Any]] = (),
    offline_proof: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root = _campaign_root(campaign_root)
    now_ns = _positive_int(now_ns, "status observation time")
    reasons = list(initial_reasons)
    evidence = [dict(row) for row in initial_evidence]
    predicates = _empty_predicates()
    predicates["release_current"] = release.valid
    if not release.valid:
        reasons.append("CURRENT_RELEASE_INVALID")
        evidence.append(
            _evidence_row("current_release", detail=release.error)
        )

    controller_status = {
        "observed_at_unix_ns": None,
        "fresh_get_observed_at_unix_ns": None,
        "delivery_transaction_id": None,
        "uploaded": {
            "expected": release.expected_triplet_sha256,
            "observed": None,
            "verified": False,
        },
        "readback": {
            "expected": release.expected_triplet_sha256,
            "observed": None,
            "verified": False,
        },
        "loaded": {
            "expected": release.expected_loaded_program,
            "observed": None,
            "verified": False,
        },
        "tp_runtime_identity": {
            "expected": release.expected_tp_runtime_identity,
            "observed": None,
            "verified": False,
        },
        "rtde_recipe": {"fields": None, "types": None, "verified": False},
        "delivery_evidence": None,
    }
    bridge_status = {
        "pid": None,
        "starttime_ticks": None,
        "alive": False,
        "heartbeat_at_unix_ns": None,
        "heartbeat_age_ns": None,
        "heartbeat_fresh": False,
    }
    lease_status: dict[str, Any] = {
        "present": False,
        "valid": False,
        "fingerprint": None,
        "campaign_id": None,
        "campaign_fingerprint": None,
        "safety_envelope_sha256": None,
        "expires_at_unix_ns": None,
        "revoked_at_unix_ns": None,
        "revocation_reason": None,
    }
    outcome = {
        "live_proven": False,
        "trial_id": None,
        "evidence": None,
    }
    terminal_status = {
        "completed": False,
        "reason": None,
        "observed_at_unix_ns": None,
        "runner_exit_code": None,
    }
    attestation_status = {
        "present": attestation is not None,
        "sequence": None,
        "run_id": None,
        "campaign_id": None,
        "observed_at_unix_ns": None,
        "path": None if pointer is None else pointer.get("attestation_path"),
        "sha256": None if pointer is None else pointer.get("attestation_sha256"),
    }

    state: str | None = None
    if attestation is None:
        if offline_proof is not None:
            offline_reference_current = _reference_is_current(
                root, offline_proof["evidence"]
            )
            offline_binding_matches = (
                offline_proof["manifest_sha256"] == release.manifest_sha256
                and offline_proof["source_fingerprint"] == release.source_fingerprint
                and offline_proof["launcher_sha256"] == release.launcher_sha256
            )
            if not offline_reference_current:
                reasons.append("OFFLINE_EVIDENCE_MISSING")
            if not offline_binding_matches:
                reasons.append("OFFLINE_BINDING_MISMATCH")
            predicates["offline_proven"] = all(
                (release.valid, offline_reference_current, offline_binding_matches)
            )
            if predicates["offline_proven"]:
                state = "OFFLINE_PROVEN"
            evidence.append(
                _evidence_row("offline_qualification", offline_proof["evidence"])
            )
        elif not {
            "CURRENT_OBSERVATION_MISSING",
            "LAUNCH_ATTEMPT_FAILED",
            "OBSERVATION_POINTER_INVALID",
            "OBSERVED_ATTESTATION_INVALID",
        }.intersection(reasons):
            reasons.append("CURRENT_OBSERVATION_MISSING")
    else:
        row = validate_observed_attestation(attestation)
        attestation_status.update(
            {
                "sequence": row["sequence"],
                "run_id": row["run_id"],
                "campaign_id": row["campaign_id"],
                "observed_at_unix_ns": row["observed_at_unix_ns"],
            }
        )
        bindings = row["bindings"]
        offline = row["offline"]
        process = row["process"]
        controller = row["controller"]
        mailbox = row["mailbox"]
        events = row["events"]
        terminal_event = events["campaign_terminal"]

        manifest_matches = bindings["manifest_sha256"] == release.manifest_sha256
        source_matches = bindings["source_fingerprint"] == release.source_fingerprint
        launcher_matches = bindings["launcher_sha256"] == release.launcher_sha256
        safety_matches = (
            bindings["safety_envelope_sha256"]
            == release.safety_envelope_sha256
        )
        if not manifest_matches:
            reasons.append("OBSERVATION_RELEASE_MISMATCH")
        if not source_matches:
            reasons.append("SOURCE_BINDING_MISMATCH")
        if not launcher_matches:
            reasons.append("LAUNCHER_BINDING_MISMATCH")
        if not safety_matches:
            reasons.append("SAFETY_ENVELOPE_BINDING_MISMATCH")

        offline_reference_current = _reference_is_current(root, offline["evidence"])
        if not offline_reference_current:
            reasons.append("OFFLINE_EVIDENCE_MISSING")
        offline_binding_matches = (
            offline["manifest_sha256"] == bindings["manifest_sha256"]
            and offline["source_fingerprint"] == bindings["source_fingerprint"]
            and offline["launcher_sha256"] == bindings["launcher_sha256"]
            and offline["environment_sha256"] == bindings["environment_sha256"]
            and offline["process_tree_fingerprint"]
            == bindings["process_tree_fingerprint"]
        )
        if not offline_binding_matches:
            reasons.append("OFFLINE_BINDING_MISMATCH")
        predicates["offline_proven"] = all(
            (
                release.valid,
                manifest_matches,
                source_matches,
                launcher_matches,
                safety_matches,
                offline_reference_current,
                offline_binding_matches,
            )
        )

        bridge_pid = process["bridge_pid"]
        bridge_starttime = process["bridge_starttime_ticks"]
        current_starttime = proc_starttime_reader(bridge_pid)
        bridge_alive = current_starttime == bridge_starttime
        predicates["bridge_process_alive"] = bridge_alive
        if current_starttime is None:
            reasons.append("BRIDGE_PROCESS_DEAD")
        elif not bridge_alive:
            reasons.append("BRIDGE_PID_REUSED")
        heartbeat_at = process["heartbeat_at_unix_ns"]
        heartbeat_fresh = bridge_alive and _age_fresh(
            now_ns, heartbeat_at, BRIDGE_HEARTBEAT_MAX_AGE_NS
        )
        predicates["bridge_heartbeat_fresh"] = heartbeat_fresh
        if not heartbeat_fresh:
            reasons.append("BRIDGE_HEARTBEAT_STALE")
        process_evidence_current = _reference_is_current(root, process["evidence"])
        process_binding_matches = (
            process["process_tree_fingerprint"]
            == bindings["process_tree_fingerprint"]
        )
        if not process_evidence_current:
            reasons.append("PROCESS_TREE_EVIDENCE_INVALID")
        if not process_binding_matches:
            reasons.append("PROCESS_TREE_BINDING_MISMATCH")
        predicates["single_writer"] = process["writer_pids"] == [bridge_pid]
        if not predicates["single_writer"]:
            reasons.append("MULTIPLE_WRITERS")
        bridge_status.update(
            {
                "pid": bridge_pid,
                "starttime_ticks": bridge_starttime,
                "alive": bridge_alive,
                "heartbeat_at_unix_ns": heartbeat_at,
                "heartbeat_age_ns": max(0, now_ns - heartbeat_at),
                "heartbeat_fresh": heartbeat_fresh,
            }
        )

        controller_evidence_current = _reference_is_current(
            root, controller["evidence"]
        )
        delivery_evidence_current = _reference_is_current(
            root, controller["delivery_observation"]
        )
        predicates["controller_fresh"] = (
            controller_evidence_current
            and delivery_evidence_current
            and _age_fresh(
            now_ns,
            controller["observed_at_unix_ns"],
            CONTROLLER_OBSERVATION_MAX_AGE_NS,
            )
        )
        if not predicates["controller_fresh"]:
            reasons.append("CONTROLLER_OBSERVATION_STALE")
        delivery_provenance_current = _delivery_provenance_is_current(
            root,
            controller["delivery_observation"],
            transaction_id=controller["delivery_transaction_id"],
            observed_at_unix_ns=controller[
                "fresh_get_observed_at_unix_ns"
            ],
        )
        predicates["controller_fresh_get"] = (
            delivery_provenance_current
            and _age_fresh(
                now_ns,
                controller["fresh_get_observed_at_unix_ns"],
                CONTROLLER_FRESH_GET_MAX_AGE_NS,
            )
        )
        if not delivery_provenance_current:
            reasons.append("CONTROLLER_FRESH_GET_BINDING_MISMATCH")
        elif not predicates["controller_fresh_get"]:
            reasons.append("CONTROLLER_FRESH_GET_STALE")
        expected_triplet = release.expected_triplet_sha256
        uploaded = controller["uploaded_triplet_sha256"]
        readback = controller["readback_triplet_sha256"]
        predicates["uploaded_identity_verified"] = (
            predicates["controller_fresh"]
            and predicates["controller_fresh_get"]
            and expected_triplet is not None
            and uploaded == expected_triplet
        )
        predicates["controller_triplet_verified"] = (
            predicates["controller_fresh"]
            and predicates["controller_fresh_get"]
            and expected_triplet is not None
            and readback == expected_triplet
        )
        if not predicates["uploaded_identity_verified"]:
            reasons.append("UPLOADED_TRIPLET_MISMATCH")
        if not predicates["controller_triplet_verified"]:
            reasons.append("CONTROLLER_READBACK_MISMATCH")
        loaded = controller["loaded_program"]
        predicates["loaded_program_verified"] = (
            predicates["controller_fresh"]
            and release.expected_loaded_program is not None
            and loaded == release.expected_loaded_program
        )
        if not predicates["loaded_program_verified"]:
            reasons.append("DASHBOARD_LOADED_PROGRAM_MISMATCH")
        play = events["play_observed_at_unix_ns"]
        observed_tp = controller["tp_runtime_identity"]
        predicates["tp_runtime_identity_verified"] = (
            predicates["controller_fresh"]
            and release.expected_tp_runtime_identity is not None
            and observed_tp == release.expected_tp_runtime_identity
        )
        # A newly loaded, STOPPED TP program has not executed the identity
        # writes yet.  Runtime identity becomes mandatory as soon as Play is
        # observed and remains mandatory before every ARM.
        if play is not None:
            if observed_tp is None:
                reasons.append("TP_RUNTIME_IDENTITY_UNAVAILABLE")
            elif not predicates["tp_runtime_identity_verified"]:
                reasons.append("TP_RUNTIME_IDENTITY_MISMATCH")
        try:
            from .runtime_identity import validate_rtde_output_recipe

            validate_rtde_output_recipe(
                controller["rtde_output_fields"], controller["rtde_output_types"]
            )
        except (ImportError, RuntimeError, ValueError):
            reasons.append("RTDE_RECIPE_CAPABILITY_MISMATCH")
        else:
            predicates["rtde_recipe_capable"] = True
        predicates["rtde_fresh"] = controller_evidence_current and _age_fresh(
            now_ns,
            controller["rtde_observed_at_unix_ns"],
            RTDE_OBSERVATION_MAX_AGE_NS,
        )
        predicates["kunwei_fresh"] = controller_evidence_current and _age_fresh(
            now_ns,
            controller["kunwei_observed_at_unix_ns"],
            KUNWEI_OBSERVATION_MAX_AGE_NS,
        )
        if not predicates["rtde_fresh"]:
            reasons.append("RTDE_STALE")
        if not predicates["kunwei_fresh"]:
            reasons.append("KUNWEI_STALE")
        controller_status = {
            "observed_at_unix_ns": controller["observed_at_unix_ns"],
            "fresh_get_observed_at_unix_ns": controller[
                "fresh_get_observed_at_unix_ns"
            ],
            "delivery_transaction_id": controller["delivery_transaction_id"],
            "uploaded": {
                "expected": expected_triplet,
                "observed": uploaded,
                "verified": predicates["uploaded_identity_verified"],
            },
            "readback": {
                "expected": expected_triplet,
                "observed": readback,
                "verified": predicates["controller_triplet_verified"],
            },
            "loaded": {
                "expected": release.expected_loaded_program,
                "observed": loaded,
                "verified": predicates["loaded_program_verified"],
            },
            "tp_runtime_identity": {
                "expected": release.expected_tp_runtime_identity,
                "observed": observed_tp,
                "verified": predicates["tp_runtime_identity_verified"],
            },
            "rtde_recipe": {
                "fields": controller["rtde_output_fields"],
                "types": controller["rtde_output_types"],
                "verified": predicates["rtde_recipe_capable"],
            },
            "delivery_evidence": controller["delivery_observation"],
        }

        mailbox_evidence_current = _reference_is_current(root, mailbox["evidence"])
        mailbox_fresh = mailbox_evidence_current and _age_fresh(
            now_ns,
            mailbox["observed_at_unix_ns"],
            MAILBOX_OBSERVATION_MAX_AGE_NS,
        )
        predicates["mailbox_clean"] = (
            mailbox_fresh
            and mailbox["pending_arm_sequence"] is None
            and mailbox["duplicate_arm_detected"] is False
        )
        if not mailbox_fresh:
            reasons.append("MAILBOX_OBSERVATION_STALE")
        elif not predicates["mailbox_clean"]:
            reasons.append("MAILBOX_DIRTY")

        lease = row["lease"]
        lease_binding_matches = False
        if lease is None:
            reasons.append("CAMPAIGN_LEASE_MISSING")
        else:
            lease_status.update(
                {
                    "present": True,
                    "fingerprint": lease["fingerprint"],
                    "campaign_id": lease["campaign_id"],
                    "campaign_fingerprint": lease["campaign_fingerprint"],
                    "safety_envelope_sha256": lease["safety_envelope_sha256"],
                    "expires_at_unix_ns": lease["expires_at_unix_ns"],
                    "revoked_at_unix_ns": lease["revoked_at_unix_ns"],
                    "revocation_reason": lease["revocation_reason"],
                }
            )
            lease_binding_matches = (
                lease["campaign_id"] == row["campaign_id"]
                and lease["manifest_sha256"] == bindings["manifest_sha256"]
                and lease["campaign_fingerprint"] == bindings["campaign_fingerprint"]
                and lease["safety_envelope_sha256"]
                == bindings["safety_envelope_sha256"]
                and manifest_matches
                and safety_matches
            )
            supervisor_starttime = proc_starttime_reader(lease["supervisor_pid"])
            supervisor_alive = supervisor_starttime is not None
            supervisor_matches = (
                supervisor_starttime == lease["supervisor_starttime_ticks"]
            )
            lease_time_valid = (
                lease["issued_at_unix_ns"] <= now_ns < lease["expires_at_unix_ns"]
            )
            lease_not_revoked = lease["revoked_at_unix_ns"] is None
            predicates["lease_valid"] = all(
                (
                    lease_binding_matches,
                    lease_time_valid,
                    lease_not_revoked,
                    supervisor_matches,
                )
            )
            lease_status["valid"] = predicates["lease_valid"]
            if not lease_binding_matches:
                reasons.append("CAMPAIGN_LEASE_FINGERPRINT_MISMATCH")
            if not lease_time_valid:
                reasons.append("CAMPAIGN_LEASE_EXPIRED")
            if not lease_not_revoked:
                reasons.append("CAMPAIGN_LEASE_REVOKED")
            if not supervisor_alive:
                reasons.append("CAMPAIGN_LEASE_SUPERVISOR_DEAD")
            elif not supervisor_matches:
                reasons.append("CAMPAIGN_LEASE_SUPERVISOR_PID_REUSED")

        recheck = events["play_identity_recheck"]
        if play is None:
            predicates["play_identity_rechecked"] = True
        elif recheck is None:
            reasons.append("PLAY_IDENTITY_RECHECK_MISSING")
        else:
            predicates["play_identity_rechecked"] = (
                recheck["readback_triplet_sha256"] == expected_triplet
                and recheck["loaded_program"] == release.expected_loaded_program
                and recheck["tp_runtime_identity"]
                == release.expected_tp_runtime_identity
            )
            if not predicates["play_identity_rechecked"]:
                reasons.append("PLAY_IDENTITY_RECHECK_FAILED")
        first_ack = events["first_arm_ack"]
        predicates["first_arm_acknowledged"] = first_ack is not None
        if play is not None and predicates["play_identity_rechecked"] and first_ack is None:
            reasons.append("FIRST_ARM_ACK_MISSING")

        play_prompt_inputs = (
            "offline_proven",
            "controller_fresh",
            "controller_fresh_get",
            "uploaded_identity_verified",
            "controller_triplet_verified",
            "loaded_program_verified",
            "rtde_recipe_capable",
            "rtde_fresh",
            "kunwei_fresh",
            "bridge_process_alive",
            "bridge_heartbeat_fresh",
            "single_writer",
            "mailbox_clean",
            "lease_valid",
            "play_identity_rechecked",
        )
        predicates["play_prompt_ready"] = (
            process_evidence_current
            and process_binding_matches
            and all(predicates[name] for name in play_prompt_inputs)
        )
        bench_inputs = play_prompt_inputs + ("tp_runtime_identity_verified",)
        predicates["bench_ready"] = (
            process_evidence_current
            and process_binding_matches
            and all(predicates[name] for name in bench_inputs)
        )
        if predicates["offline_proven"]:
            state = "OFFLINE_PROVEN"
        if (
            play is None
            and events["waiting_for_play_at_unix_ns"] is not None
            and predicates["play_prompt_ready"]
            and not predicates["tp_runtime_identity_verified"]
        ):
            state = "WAITING_FOR_IDENTITY_PLAY"
        if predicates["bench_ready"]:
            state = "BENCH_READY"
            if play is None and events["waiting_for_play_at_unix_ns"] is not None:
                state = "WAITING_FOR_PLAY"
            if play is not None and first_ack is not None:
                state = "RUNNING"

        trial = events["trial_completion"]
        next_ack = events["next_arm_ack"]
        if trial is not None and next_ack is not None:
            trial_evidence_current = _reference_is_current(root, trial["evidence"])
            heartbeat_covers_ack = heartbeat_at >= next_ack["observed_at_unix_ns"]
            outcome["live_proven"] = trial_evidence_current and heartbeat_covers_ack
            outcome["trial_id"] = trial["trial_id"]
            outcome["evidence"] = trial["evidence"]
            if not outcome["live_proven"]:
                reasons.append("LIVE_EVIDENCE_INVALID")

        if row["external_blocker"] is not None:
            external = row["external_blocker"]
            if _reference_is_current(root, external["evidence"]) and _age_fresh(
                now_ns,
                external["observed_at_unix_ns"],
                CONTROLLER_OBSERVATION_MAX_AGE_NS,
            ):
                reasons.append(external["reason_code"])
                evidence.append(
                    _evidence_row("external_blocker", external["evidence"])
                )
            else:
                reasons.append("UNKNOWN_OBSERVATION")

        if terminal_event is not None:
            terminal_status.update(
                {
                    "reason": terminal_event["reason"],
                    "observed_at_unix_ns": terminal_event[
                        "observed_at_unix_ns"
                    ],
                    "runner_exit_code": terminal_event["runner_exit_code"],
                }
            )
            terminal_integrity = all(
                (
                    outcome["live_proven"],
                    predicates["offline_proven"],
                    process_evidence_current,
                    process_binding_matches,
                    process["writer_pids"] == [bridge_pid],
                    controller_evidence_current,
                    delivery_evidence_current,
                    uploaded == expected_triplet,
                    readback == expected_triplet,
                    loaded == release.expected_loaded_program,
                    observed_tp == release.expected_tp_runtime_identity,
                    predicates["rtde_recipe_capable"],
                    mailbox_evidence_current,
                    mailbox["pending_arm_sequence"] is None,
                    mailbox["duplicate_arm_detected"] is False,
                    predicates["play_identity_rechecked"],
                    lease is not None,
                    lease_binding_matches,
                )
            )
            if terminal_integrity:
                terminal_status["completed"] = True
                reasons = [
                    reason
                    for reason in reasons
                    if reason not in TERMINAL_VOLATILE_REASON_CODES
                ]
                for name in predicates:
                    if name not in {"release_current", "offline_proven"}:
                        predicates[name] = False
                state = "OFFLINE_PROVEN" if predicates["offline_proven"] else None

        evidence.extend(
            (
                _evidence_row("offline_qualification", offline["evidence"]),
                _evidence_row("process_tree", process["evidence"]),
                _evidence_row("controller", controller["evidence"]),
                _evidence_row("delivery", controller["delivery_observation"]),
                _evidence_row("mailbox", mailbox["evidence"]),
            )
        )

    ordered_reasons = _ordered_reasons(reasons)
    live_proven = bool(outcome["live_proven"])
    return {
        "schema": GOVERNED_STATUS_SCHEMA,
        "generated_at_unix_ns": now_ns,
        "transition_actor": FSM_TRANSITION_ACTOR,
        "release": {
            "sha256": release.manifest_sha256,
            "manifest_path": release.manifest_path,
            "program_id": release.program_id,
            "release_stage_id": release.release_stage_id,
            "valid": release.valid,
            "error": release.error,
        },
        "state": state,
        "predicates": predicates,
        "bridge": bridge_status,
        "controller": controller_status,
        "campaign_lease": lease_status,
        "attestation": attestation_status,
        "terminal": terminal_status,
        "blocker": {
            "class": _blocker_class(ordered_reasons),
            "reason_codes": ordered_reasons,
            "evidence": evidence,
        },
        "outcome": outcome,
        "next_action": (
            "campaign_complete"
            if terminal_status["completed"] and not ordered_reasons
            else _next_action(state, ordered_reasons, live_proven)
        ),
    }


def _environment_status(
    experiment_root: Path,
) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    from .runtime_functional_gates import (
        RuntimeFunctionalGateError,
        load_gpu_functional_attestation,
    )
    from .runtime_installation import load_runtime_pointer_identity, runtime_status
    from .source_closure import SourceClosureError, production_source_closure_report

    observed = dict(runtime_status())
    try:
        production_source_closure_report(experiment_root)
    except SourceClosureError as exc:
        reason = "ACTIVE_SOURCE_CLOSURE_UNRESOLVED"
        detail = exc.detail
    else:
        reason = observed.get("reason_code")
        detail = observed.get("detail")
    gpu_reference: dict[str, str] | None = None
    gpu_functional_proven = False
    if reason is None:
        try:
            from .release_identity import load_current_release

            release = load_current_release(experiment_root)
            required = release.runtime_environment["required_environment_id"]
            if (
                observed.get("required_environment_id") != required
                or observed.get("observed_environment_id") != required
            ):
                reason = "RUNTIME_LOCK_MISMATCH"
                detail = "current release and promoted runtime environment IDs differ"
            else:
                pointer = load_runtime_pointer_identity()
                _payload, gpu_reference = load_gpu_functional_attestation(
                    runtime_pointer=pointer
                )
                gpu_functional_proven = True
        except RuntimeFunctionalGateError as exc:
            reason = "GPU_FUNCTIONAL_GATE_MISSING"
            detail = str(exc)
        except Exception:
            # Release errors retain their more precise CURRENT_RELEASE_INVALID reason.
            pass
    observed.update(
        {
            "gpu_identity_ready": bool(
                observed.get("host_contract_ready") and reason != "GPU_IDENTITY_MISMATCH"
            ),
            "gpu_functional_proven": gpu_functional_proven,
            "gpu_functional_evidence": gpu_reference,
            "environment_attestation_sha256": None,
            "blocker": {"reason_code": reason, "detail": detail},
        }
    )
    reasons = [] if reason is None else [str(reason)]
    evidence = (
        []
        if reason is None
        else [_evidence_row("runtime_environment", detail=str(detail))]
    )
    return observed, reasons, evidence


def resolve_governed_status(
    experiment_root: Path,
    campaign_root: Path,
    *,
    now_ns: int | None = None,
    proc_starttime_reader: Callable[[int], int | None] = read_proc_starttime_ticks,
    integrity_errors: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    environment, environment_reasons, environment_evidence = _environment_status(
        experiment_root
    )
    release = load_current_release_snapshot(experiment_root)
    observed_now = time.time_ns() if now_ns is None else now_ns
    reasons: list[str] = list(environment_reasons)
    evidence: list[dict[str, Any]] = list(environment_evidence)
    for role, detail in sorted((integrity_errors or {}).items()):
        reason = {
            "queue": "QUEUE_INTEGRITY_ERROR",
            "attempt_ledger": "ATTEMPT_LEDGER_INTEGRITY_ERROR",
        }.get(role, "LEGACY_STATE_INTEGRITY_ERROR")
        reasons.append(reason)
        evidence.append(_evidence_row(role, detail=detail))
    offline_proof: Mapping[str, Any] | None = None
    try:
        offline_proof = _load_current_offline_proof(
            experiment_root, campaign_root, release
        )
    except GovernanceError as exc:
        reasons.append("OFFLINE_EVIDENCE_MISSING")
        evidence.append(
            _evidence_row("offline_qualification", detail=f"{type(exc).__name__}:{exc}")
        )
    attestation: Mapping[str, Any] | None = None
    pointer: Mapping[str, Any] | None = None
    try:
        attestation, pointer = load_current_observation(campaign_root)
    except ObservedAttestationError as exc:
        reasons.append("OBSERVED_ATTESTATION_INVALID")
        evidence.append(
            _evidence_row("observed_attestation", detail=f"{type(exc).__name__}:{exc}")
        )
    except ObservationPointerError as exc:
        pointer_path = _campaign_root(campaign_root) / "governance/current.json"
        if pointer_path.exists():
            reasons.append("OBSERVATION_POINTER_INVALID")
            evidence.append(
                _evidence_row("current_observation", detail=f"{type(exc).__name__}:{exc}")
            )

    launch_attempt: Mapping[str, Any] | None = None
    launch_pointer: Mapping[str, Any] | None = None
    launch_status: dict[str, Any] = {
        "present": False,
        "sequence": None,
        "attempt_id": None,
        "state": None,
        "phase": None,
        "observed_at_unix_ns": None,
        "manifest_sha256": None,
        "exit_code": None,
        "reason_code": None,
        "detail": None,
        "superseded": False,
        "superseded_by_run_id": None,
        "path": None,
        "sha256": None,
    }
    try:
        launch_attempt, launch_pointer = load_current_launch_attempt(campaign_root)
    except LaunchAttemptAttestationError as exc:
        reasons.append("LAUNCH_ATTEMPT_ATTESTATION_INVALID")
        evidence.append(
            _evidence_row(
                "launch_attempt", detail=f"{type(exc).__name__}:{exc}"
            )
        )
    except LaunchAttemptPointerError as exc:
        launch_pointer_path = (
            _campaign_root(campaign_root) / "governance/current-launch.json"
        )
        if launch_pointer_path.exists():
            reasons.append("LAUNCH_ATTEMPT_POINTER_INVALID")
            evidence.append(
                _evidence_row(
                    "launch_attempt_pointer",
                    detail=f"{type(exc).__name__}:{exc}",
                )
            )

    if launch_attempt is not None and launch_pointer is not None:
        launch_reference = {
            "path": launch_pointer["attestation_path"],
            "sha256": launch_pointer["attestation_sha256"],
        }
        observation_supersedes = bool(
            attestation is not None
            and attestation["run_id"] == launch_attempt["attempt_id"]
            and attestation["observed_at_unix_ns"]
            >= launch_attempt["observed_at_unix_ns"]
        )
        release_supersedes = bool(
            launch_attempt["manifest_sha256"] is not None
            and release.valid
            and launch_attempt["manifest_sha256"] != release.manifest_sha256
        )
        superseded = observation_supersedes or release_supersedes
        launch_status.update(
            {
                "present": True,
                "sequence": launch_attempt["sequence"],
                "attempt_id": launch_attempt["attempt_id"],
                "state": launch_attempt["state"],
                "phase": launch_attempt["phase"],
                "observed_at_unix_ns": launch_attempt["observed_at_unix_ns"],
                "manifest_sha256": launch_attempt["manifest_sha256"],
                "exit_code": launch_attempt["exit_code"],
                "reason_code": launch_attempt["reason_code"],
                "detail": launch_attempt["detail"],
                "superseded": superseded,
                "superseded_by_run_id": (
                    attestation["run_id"] if observation_supersedes else None
                ),
                "path": launch_reference["path"],
                "sha256": launch_reference["sha256"],
            }
        )
        active_failure = launch_attempt["state"] == "FAILED" and not superseded
        if active_failure:
            reasons.append("LAUNCH_ATTEMPT_FAILED")
            evidence.append(
                _evidence_row(
                    "launch_attempt",
                    launch_reference,
                    detail=(
                        f"attempt={launch_attempt['attempt_id']} "
                        f"phase={launch_attempt['phase']} "
                        f"exit_code={launch_attempt['exit_code']}"
                    ),
                )
            )
            external = launch_attempt["external_evidence"]
            if external is not None and _reference_is_current(campaign_root, external):
                reasons.append(launch_attempt["reason_code"])
                evidence.append(_evidence_row("launch_external", external))
            elif external is not None:
                reasons.append("UNKNOWN_OBSERVATION")
            # A failed pre-runtime attempt is newer than any retained live
            # observation.  Do not reinterpret expected stale processes from
            # the preceding attempt as this launch failure's root cause.
            if attestation is not None:
                attestation = None
                pointer = None

    status = reduce_observed_attestation(
        release,
        attestation,
        campaign_root=campaign_root,
        now_ns=observed_now,
        proc_starttime_reader=proc_starttime_reader,
        pointer=pointer,
        initial_reasons=reasons,
        initial_evidence=evidence,
        offline_proof=offline_proof,
    )
    if environment_reasons:
        status["predicates"]["offline_proven"] = False
        status["predicates"]["play_prompt_ready"] = False
        status["predicates"]["bench_ready"] = False
        status["state"] = None
        status["outcome"]["live_proven"] = False
    elif offline_proof is not None:
        environment["environment_attestation_sha256"] = offline_proof[
            "evidence"
        ]["sha256"]
    status["environment"] = environment
    status["next_action"] = (
        _next_action(
            status["state"],
            status["blocker"]["reason_codes"],
            bool(status["outcome"]["live_proven"]),
        )
        if environment_reasons
        else status["next_action"]
    )
    status["launch_attempt"] = launch_status
    return status


def invalidation_predicates(event: str) -> tuple[str, ...]:
    try:
        return INVALIDATION_TABLE[event]
    except KeyError as exc:
        raise GovernanceError(f"unknown invalidation event: {event}") from exc


__all__ = [
    "BLOCKER_CLASSES",
    "BRIDGE_HEARTBEAT_MAX_AGE_NS",
    "CAMPAIGN_LEASE_SCHEMA",
    "CONTROLLER_FRESH_GET_MAX_AGE_NS",
    "CONTROLLER_OBSERVATION_MAX_AGE_NS",
    "CURRENT_LAUNCH_ATTEMPT_POINTER_SCHEMA",
    "CURRENT_OBSERVATION_POINTER_SCHEMA",
    "CurrentReleaseSnapshot",
    "FSM_TRANSITION_ACTOR",
    "FSM_STATES",
    "GOVERNED_STATUS_SCHEMA",
    "GovernanceError",
    "INVALIDATION_TABLE",
    "KUNWEI_OBSERVATION_MAX_AGE_NS",
    "LAUNCH_ATTEMPT_PHASES",
    "LAUNCH_ATTEMPT_SCHEMA",
    "LAUNCH_ATTEMPT_STATES",
    "LaunchAttemptAttestationError",
    "LaunchAttemptPointerError",
    "MAILBOX_OBSERVATION_MAX_AGE_NS",
    "OBSERVED_ATTESTATION_SCHEMA",
    "ObservationPointerError",
    "ObservedAttestationError",
    "REASON_ORDER",
    "RTDE_OBSERVATION_MAX_AGE_NS",
    "TRANSITION_TABLE",
    "build_campaign_lease",
    "campaign_lease_fingerprint",
    "invalidation_predicates",
    "load_current_launch_attempt",
    "load_current_observation",
    "load_current_release_snapshot",
    "publish_launch_attempt",
    "publish_observed_attestation",
    "read_proc_starttime_ticks",
    "reduce_observed_attestation",
    "resolve_governed_status",
    "validate_campaign_lease",
    "validate_launch_attempt",
    "validate_observed_attestation",
]
