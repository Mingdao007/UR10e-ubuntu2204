"""Typed machine binding for distinct receiver and optimizer plan identities.

The legacy V3 writer remains in its pinned module for V3 compatibility.  New
R006/R008 production-chain callers use this schema so a receiver plan digest
can never be mistaken for an optimizer candidate-plan digest.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path

from step5d_autotune_batch_plan import load_plan
from step5d_autotune_v3.state import atomic_json


RECEIVER_PLAN_SCHEMA = "step5d.parameter-receiver/launch-plan-v1"
MACHINE_BINDING_SCHEMA_V4 = "step5d_autotune_campaign_binding_v4"


def _sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_regular_file(path: Path, role: str) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"{role} must be a real file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{role} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{role} must be a JSON object")
    return value


def _plan_descriptor(
    path: Path,
    *,
    role: str,
    campaign_id: str,
) -> dict[str, object]:
    if role == "receiver":
        plan = _json_regular_file(path, "parameter receiver plan")
        revision = plan.get("revision")
        if (
            plan.get("schema") != RECEIVER_PLAN_SCHEMA
            or plan.get("campaign_id") != campaign_id
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
        ):
            raise RuntimeError("machine campaign binding requires an exact receiver plan")
        schema = str(plan["schema"])
    elif role == "optimizer":
        try:
            plan = load_plan(path, campaign_id=campaign_id)
        except Exception as exc:
            raise RuntimeError(
                "machine campaign binding requires an exact optimizer plan"
            ) from exc
        revision = plan.revision
        schema = str(plan.payload["schema_version"])
    else:
        raise RuntimeError(f"unsupported machine binding plan role: {role}")
    return {
        "schema": schema,
        "revision": revision,
        "sha256": _sha256_path(path),
    }


def write_machine_campaign_binding(
    path: Path,
    *,
    campaign_id: str,
    campaign_epoch: int,
    campaign_fingerprint: str,
    candidate_plan_path: Path | None = None,
    receiver_plan_path: Path | None = None,
    optimizer_plan_path: Path | None = None,
    trial_overlay_plan_path: Path,
    binding_source: str,
) -> dict[str, object]:
    """Persist one v4 binding with role-specific plan identity descriptors."""

    if isinstance(campaign_epoch, bool) or not isinstance(campaign_epoch, int) or campaign_epoch < 1:
        raise RuntimeError("machine campaign binding epoch is invalid")
    if not isinstance(campaign_fingerprint, str) or not re.fullmatch(
        r"[0-9a-f]{64}", campaign_fingerprint
    ):
        raise RuntimeError("machine campaign binding fingerprint is malformed")
    if not isinstance(binding_source, str) or not binding_source.strip():
        raise RuntimeError("machine campaign binding source is empty")
    if trial_overlay_plan_path.is_symlink() or not trial_overlay_plan_path.is_file():
        raise RuntimeError("trial overlay plan must be a real file")

    if candidate_plan_path is not None:
        if receiver_plan_path is not None or optimizer_plan_path is not None:
            raise RuntimeError("candidate_plan_path cannot be mixed with typed plan paths")
        candidate_payload = _json_regular_file(candidate_plan_path, "candidate plan")
        if candidate_payload.get("schema") == RECEIVER_PLAN_SCHEMA:
            receiver_plan_path = candidate_plan_path
        else:
            optimizer_plan_path = candidate_plan_path
    if receiver_plan_path is None and optimizer_plan_path is None:
        raise RuntimeError("machine campaign binding requires a receiver or optimizer plan")

    receiver_descriptor = (
        None
        if receiver_plan_path is None
        else _plan_descriptor(
            receiver_plan_path,
            role="receiver",
            campaign_id=campaign_id,
        )
    )
    optimizer_descriptor = (
        None
        if optimizer_plan_path is None
        else _plan_descriptor(
            optimizer_plan_path,
            role="optimizer",
            campaign_id=campaign_id,
        )
    )
    payload = {
        "schema_version": MACHINE_BINDING_SCHEMA_V4,
        "campaign_id": campaign_id,
        "campaign_epoch": campaign_epoch,
        "campaign_fingerprint": campaign_fingerprint,
        "receiver_plan": receiver_descriptor,
        "optimizer_plan": optimizer_descriptor,
        "trial_overlay_plan_sha256": _sha256_path(trial_overlay_plan_path),
        "binding_source": binding_source,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    atomic_json(path, payload)
    return payload


__all__ = [
    "MACHINE_BINDING_SCHEMA_V4",
    "RECEIVER_PLAN_SCHEMA",
    "write_machine_campaign_binding",
]
