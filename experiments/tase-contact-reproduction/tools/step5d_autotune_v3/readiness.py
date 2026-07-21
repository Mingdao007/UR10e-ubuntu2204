"""Dynamic six-dimensional readiness for the selected Step5d V3 release.

The historical pre-live verifier remains an evidence auditor.  This module is
the runtime owner for the much smaller question of what the selected release
can do *now*.  Missing optional runtime artifacts produce false dimensions;
malformed or mismatched artifacts fail closed and are named as blockers.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from ur10e_experiment_runtime.identity import load_strict_json

from .arming import (
    ArmingContext,
    BridgeStartContext,
    load_bridge_start_context,
    load_campaign_arming_context,
)
from .profile import ContractViolation, active_identity_snapshot, load_contract
from .runtime_profile import CONTROL_PROFILE_ID, RELEASE_STAGE_ID, TP_PROGRAM_ID


READINESS_SCHEMA = "step5d.autotune-v3/release-readiness-v1"
RUNTIME_READINESS_SCHEMA = "step5d.autotune-v3/runtime-readiness-v1"
DIMENSIONS = (
    "selected_release",
    "deployment_ready",
    "bridge_start_ready",
    "bridge_process_ready",
    "motion_arm_ready",
    "campaign_ready",
)
_V1_STAGE_ID = "step5d_strict_rnn_autotune_v1"


class ReleaseReadinessError(RuntimeError):
    """The selected V3 release cannot truthfully claim the requested state."""


def _sha256(path: Path, role: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise ReleaseReadinessError(f"{role} is missing or unsafe")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path, role: str) -> Mapping[str, Any]:
    try:
        payload = load_strict_json(path)
    except (OSError, ValueError) as exc:
        raise ReleaseReadinessError(f"{role} is invalid: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ReleaseReadinessError(f"{role} must be an object")
    return payload


def _selected_v3(root: Path) -> Mapping[str, Any]:
    current = _load(root / "config/current_stage.json", "current selector")
    if (
        current.get("current_stage_id") != RELEASE_STAGE_ID
        or current.get("program") != RELEASE_STAGE_ID
        or current.get("selection_state") != "current"
    ):
        raise ReleaseReadinessError("V3 is not the unique current selector")
    table = _load(root / "config/step5_stage_table.json", "stage table")
    rows = {
        row.get("id"): row
        for row in table.get("stages", [])
        if isinstance(row, Mapping)
    }
    v3 = rows.get(RELEASE_STAGE_ID) or {}
    v1 = rows.get(_V1_STAGE_ID) or {}
    if not (
        v3.get("active") is True
        and v3.get("bridge") is True
        and (v3.get("current_binding") or {}).get("is_current") is True
        and v1.get("active") is False
        and v1.get("bridge") is False
        and (v1.get("current_binding") or {}).get("is_current") is False
    ):
        raise ReleaseReadinessError("stage table permits a non-V3 launch route")
    protocol = _load(root / "config/tase_protocol_table.json", "protocol table")
    profile = (protocol.get("experiment_profiles") or {}).get("Step5.step5d_rnn") or {}
    compatibility = _load(root / "config/step5d/current.json", "Step5d current pointer")
    if (
        profile.get("current_program") != RELEASE_STAGE_ID
        or compatibility.get("program") != RELEASE_STAGE_ID
        or compatibility.get("tp_program_id") != TP_PROGRAM_ID
        or compatibility.get("selection_state") != "current"
    ):
        raise ReleaseReadinessError("V3 selector surfaces differ")
    return compatibility


def _deployment_state(
    root: Path,
    identity: Mapping[str, Any],
) -> tuple[bool, Path, str]:
    table = _load(root / "config/step5_stage_table.json", "stage table")
    rows = [
        row
        for row in table.get("stages", [])
        if isinstance(row, Mapping) and row.get("id") == RELEASE_STAGE_ID
    ]
    if len(rows) != 1:
        raise ReleaseReadinessError("V3 package owner is ambiguous")
    package = rows[0].get("package_delivery") or {}
    relative = package.get("controller_readback_manifest")
    if not isinstance(relative, str) or not relative:
        raise ReleaseReadinessError("V3 controller readback path is missing")
    readback_path = root / relative
    readback_sha256 = _sha256(readback_path, "V3 controller readback")
    readback = _load(readback_path, "V3 controller readback")
    deployment_ready = bool(
        readback.get("schema") == "step5d.autotune.controller-readback/v3"
        and readback.get("verified") is True
        and readback.get("program") == TP_PROGRAM_ID
        and readback.get("control_profile_id") == CONTROL_PROFILE_ID
        and readback.get("triplet_sha256") == identity.get("local_triplet_sha256")
        and identity.get("controller_readback_triplet_sha256")
        == identity.get("local_triplet_sha256")
    )
    return deployment_ready, readback_path, readback_sha256


def _pid_alive(pid: Any) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, PermissionError):
        return False
    return True


def _runtime_state(
    path: Path,
    *,
    bridge_context: BridgeStartContext,
    bridge_context_sha256: str,
    arming_context: ArmingContext | None,
    arming_context_sha256: str | None,
) -> tuple[bool, bool, bool]:
    payload = _load(path, "runtime readiness")
    required = {
        "schema",
        "selected_release",
        "deployment_ready",
        "bridge_start_ready",
        "bridge_process_ready",
        "motion_arm_ready",
        "campaign_ready",
        "identity",
        "bridge_start_context_sha256",
        "campaign_arming_context_sha256",
        "bridge_process_pid",
        "bridge_launch_id",
        "release_fingerprint",
    }
    if set(payload) != required:
        raise ReleaseReadinessError("runtime readiness fields differ")
    launch_id = payload.get("bridge_launch_id")
    if (
        payload.get("schema") != RUNTIME_READINESS_SCHEMA
        or payload.get("selected_release") != RELEASE_STAGE_ID
        or payload.get("deployment_ready") is not True
        or payload.get("bridge_start_ready") is not True
        or payload.get("identity") != bridge_context.identity
        or payload.get("bridge_start_context_sha256") != bridge_context_sha256
        or not isinstance(launch_id, str)
        or len(launch_id) != 32
        or any(character not in "0123456789abcdef" for character in launch_id)
    ):
        raise ReleaseReadinessError("runtime readiness release identity differs")
    process_ready = bool(
        payload.get("bridge_process_ready") is True
        and _pid_alive(payload.get("bridge_process_pid"))
    )
    if not process_ready:
        return False, False, False
    if arming_context is None:
        if (
            payload.get("motion_arm_ready") is not False
            or payload.get("campaign_ready") is not False
            or payload.get("campaign_arming_context_sha256") is not None
            or payload.get("release_fingerprint") is not None
        ):
            raise ReleaseReadinessError("NO_ARM runtime readiness overclaims capability")
        return True, False, False
    if (
        payload.get("campaign_arming_context_sha256") != arming_context_sha256
        or payload.get("release_fingerprint") != arming_context.release_fingerprint
    ):
        raise ReleaseReadinessError("runtime readiness arming identity differs")
    motion_ready = payload.get("motion_arm_ready") is True
    campaign_ready = payload.get("campaign_ready") is True and motion_ready
    return True, motion_ready, campaign_ready


def resolve_release_readiness(
    root: Path,
    *,
    bridge_start_context_path: Path | None = None,
    campaign_arming_context_path: Path | None = None,
    runtime_readiness_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resolve readiness without turning absent future artifacts into errors."""

    root = root.expanduser().resolve(strict=True)
    compatibility = _selected_v3(root)
    try:
        contract = load_contract(
            root / "config/step5/step5d_autotune_v3_control_contract.json"
        )
        identity = active_identity_snapshot(contract, experiment_root=root)
    except (ContractViolation, ValueError) as exc:
        raise ReleaseReadinessError(f"active release identity is invalid: {exc}") from exc
    deployment_ready, readback_path, readback_sha256 = _deployment_state(
        root, identity
    )
    blockers: list[str] = []
    if not deployment_ready:
        blockers.append("requires_matching_v3_tp_readback")
    tp_program_disposition = compatibility.get("tp_program_disposition")
    tp_program_start_allowed = tp_program_disposition != "known_incompatible_do_not_retry"
    if not tp_program_start_allowed:
        blockers.append("selected_tp_program_known_incompatible_do_not_retry")
        if compatibility.get("tp_program_id") == "step5d_strict_rnn_autotune_v3_r005":
            blockers.append("r005_post_ack_csv_schema_timeout_incident")
    host_runtime_disposition = compatibility.get("host_runtime_disposition")
    host_runtime_start_allowed = (
        host_runtime_disposition
        == "verified_r006_cross_process_direct_arm1_arm2_offline"
    )
    if not host_runtime_start_allowed:
        if host_runtime_disposition == (
            "blocked_r005_post_ack_csv_schema_timeout_incident"
        ):
            blockers.append("r005_post_ack_csv_schema_timeout_incident")
        blockers.append("requires_r006_offline_release")
    if (
        compatibility.get("local_candidate_tp_program_id")
        == "step5d_strict_rnn_autotune_v3_r006"
        and compatibility.get("local_candidate_tp_disposition")
        != "controller_readback_verified_promoted_current"
    ):
        blockers.append("requires_r006_controller_readback")

    bridge_context: BridgeStartContext | None = None
    bridge_context_sha256: str | None = None
    if bridge_start_context_path is not None:
        path = bridge_start_context_path.expanduser().absolute()
        if path.exists() or path.is_symlink():
            try:
                bridge_context_sha256 = _sha256(path, "bridge-start context")
                bridge_context = load_bridge_start_context(
                    path,
                    expected_static_identity=identity,
                    expected_deployment_readback_sha256=readback_sha256,
                )
                if not deployment_ready:
                    raise ReleaseReadinessError(
                        "bridge-start context cannot override deployment mismatch"
                    )
            except Exception as exc:
                blockers.append(f"invalid_bridge_start_context:{exc}")
                bridge_context = None
                bridge_context_sha256 = None
        else:
            blockers.append("requires_bridge_start_context")
    else:
        blockers.append("requires_bridge_start_context")
    bridge_start_ready = (
        bridge_context is not None
        and deployment_ready
        and tp_program_start_allowed
        and host_runtime_start_allowed
    )

    arming_context: ArmingContext | None = None
    arming_context_sha256: str | None = None
    if campaign_arming_context_path is not None:
        path = campaign_arming_context_path.expanduser().absolute()
        if path.exists() or path.is_symlink():
            try:
                arming_context_sha256 = _sha256(path, "campaign arming context")
                arming_context = load_campaign_arming_context(
                    path,
                    expected_static_identity=identity,
                    now=now,
                )
                if (
                    bridge_context is None
                    or arming_context.bridge_start != bridge_context
                ):
                    raise ReleaseReadinessError(
                        "campaign arming context differs from bridge-start context"
                    )
            except Exception as exc:
                blockers.append(f"invalid_campaign_arming_context:{exc}")
                arming_context = None
                arming_context_sha256 = None

    bridge_process_ready = False
    motion_arm_ready = False
    campaign_ready = False
    if runtime_readiness_path is not None:
        path = runtime_readiness_path.expanduser().absolute()
        if path.exists() or path.is_symlink():
            if bridge_context is None or bridge_context_sha256 is None:
                blockers.append("runtime_readiness_without_bridge_context")
            else:
                try:
                    (
                        bridge_process_ready,
                        motion_arm_ready,
                        campaign_ready,
                    ) = _runtime_state(
                        path,
                        bridge_context=bridge_context,
                        bridge_context_sha256=bridge_context_sha256,
                        arming_context=arming_context,
                        arming_context_sha256=arming_context_sha256,
                    )
                except Exception as exc:
                    blockers.append(f"invalid_runtime_readiness:{exc}")
        else:
            blockers.append("requires_running_v3_bridge")

    return {
        "schema": READINESS_SCHEMA,
        "selected_release": RELEASE_STAGE_ID,
        "deployment_ready": deployment_ready,
        "bridge_start_ready": bridge_start_ready,
        "bridge_process_ready": bridge_process_ready,
        "motion_arm_ready": motion_arm_ready,
        "campaign_ready": campaign_ready,
        "identity": identity,
        "release_identity": (
            bridge_context.identity if bridge_context is not None else None
        ),
        "release_fingerprint": (
            arming_context.release_fingerprint
            if arming_context is not None
            else None
        ),
        "controller_readback_path": str(readback_path),
        "controller_readback_sha256": readback_sha256,
        "tp_program_disposition": tp_program_disposition,
        "tp_program_start_allowed": tp_program_start_allowed,
        "host_runtime_disposition": host_runtime_disposition,
        "host_runtime_start_allowed": host_runtime_start_allowed,
        "blockers": blockers,
    }


def require_bridge_start(
    root: Path,
    bridge_start_context_path: Path,
) -> tuple[dict[str, Any], BridgeStartContext]:
    report = resolve_release_readiness(
        root,
        bridge_start_context_path=bridge_start_context_path,
    )
    if report["bridge_start_ready"] is not True:
        raise ReleaseReadinessError(";".join(report["blockers"]))
    context = load_bridge_start_context(
        bridge_start_context_path.expanduser().absolute(),
        expected_static_identity=report["identity"],
        expected_deployment_readback_sha256=report["controller_readback_sha256"],
    )
    return report, context


__all__ = [
    "DIMENSIONS",
    "READINESS_SCHEMA",
    "RUNTIME_READINESS_SCHEMA",
    "ReleaseReadinessError",
    "require_bridge_start",
    "resolve_release_readiness",
]
