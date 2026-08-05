"""B3 canary campaign identity: new fingerprint, never mainline 1db4f9bf."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from step5d_autotune_v4_r006.contracts import (
    R006Contract,
    canonical_bytes,
    load_contract,
    sha256_bytes,
)
from step5d_managed_runtime import ManagedRuntimeManifest

from .contact_search_schedule import (
    MAINLINE_FINGERPRINT,
    PROGRAM_B3,
    ContactSearchSchedule,
    load_schedule,
)

ROOT = Path(__file__).resolve().parents[2]
B3_CONTRACT_PATH = ROOT / "config/step5d/autotune_v4_r008_b3_two_stage.json"
B3_IDENTITY_SCHEMA = "step5d.autotune-v4/r008-b3-two-stage-identity-v1"


class B3IdentityError(RuntimeError):
    """B3 canary identity is inconsistent or collides with mainline."""


def compute_b3_campaign_fingerprint(
    *,
    parent_fingerprint: str,
    schedule: ContactSearchSchedule,
    program: str = PROGRAM_B3,
) -> str:
    if parent_fingerprint != MAINLINE_FINGERPRINT:
        raise B3IdentityError("parent fingerprint must be mainline 1db4f9bf")
    material = {
        "schema": B3_IDENTITY_SCHEMA,
        "program": program,
        "parent_campaign_fingerprint": parent_fingerprint,
        "schedule": schedule.as_dict(),
    }
    fingerprint = sha256_bytes(canonical_bytes(material))
    if fingerprint == MAINLINE_FINGERPRINT:
        raise B3IdentityError("canary fingerprint collided with mainline")
    return fingerprint


def build_b3_contract_document(
    *,
    parent: R006Contract,
    schedule: ContactSearchSchedule,
    campaign_fingerprint: str,
) -> dict[str, Any]:
    """Fork r006 raw with B3 program + schedule; used only for canary binding."""

    document = json.loads(json.dumps(dict(parent.raw)))  # deep copy via JSON
    document["lineage_id"] = PROGRAM_B3
    document["program"] = PROGRAM_B3
    document["revision"] = int(document.get("revision", 6)) + 1000  # canary namespace
    motion = dict(document["motion"])
    unchanged = dict(motion.get("unchanged", {}))
    unchanged["contact_search_speed_m_s"] = schedule.v_near_m_s
    unchanged["r008_b3_contact_search_schedule"] = schedule.as_dict()
    motion["unchanged"] = unchanged
    motion["r008_b3_two_stage"] = True
    document["motion"] = motion
    document["b3_canary"] = {
        "schema": B3_IDENTITY_SCHEMA,
        "parent_campaign_fingerprint": schedule.parent_campaign_fingerprint,
        "campaign_fingerprint": campaign_fingerprint,
        "overwrites_mainline_r006": False,
    }
    return document


@dataclass(frozen=True)
class R008B3Contract:
    """Duck-typed contract for B3 host/prepare (parallel to R006Contract)."""

    path: Path
    raw: Mapping[str, Any]
    sha256: str
    campaign_fingerprint: str
    runtime_manifest: ManagedRuntimeManifest
    schedule: ContactSearchSchedule
    parent_r006: R006Contract

    @property
    def program(self) -> str:
        return str(self.raw["program"])

    @property
    def target_force_n(self) -> float:
        return float(self.parent_r006.target_force_n)

    @property
    def parent_hashes(self) -> Mapping[str, str]:
        return dict(self.raw["parent_identity"]["sha256"])

    def require_offline_only(self) -> None:
        boundary = self.raw["offline_boundary"]
        if any(bool(value) for value in boundary.values()):
            raise B3IdentityError("B3 offline boundary unexpectedly enables a live action")


def load_b3_contract(
    path: Path | None = None,
    *,
    schedule_path: Path | None = None,
) -> R008B3Contract:
    """Load or synthesize the B3 canary contract bound to the schedule."""

    schedule = load_schedule(schedule_path)
    parent = load_contract(verify_source_closure=True)
    if parent.campaign_fingerprint != MAINLINE_FINGERPRINT:
        raise B3IdentityError(
            f"mainline fingerprint drifted: {parent.campaign_fingerprint[:16]}"
        )
    fingerprint = compute_b3_campaign_fingerprint(
        parent_fingerprint=parent.campaign_fingerprint,
        schedule=schedule,
        program=PROGRAM_B3,
    )
    document = build_b3_contract_document(
        parent=parent,
        schedule=schedule,
        campaign_fingerprint=fingerprint,
    )
    contract_path = B3_CONTRACT_PATH if path is None else Path(path)
    encoded = canonical_bytes(document)
    # Persist a readable canary contract for launch wiring / audit.
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    # Pretty-print for humans; sha256 is of the pretty file bytes we write.
    pretty = json.dumps(document, indent=2, sort_keys=True) + "\n"
    contract_path.write_text(pretty, encoding="utf-8")
    file_sha = sha256_bytes(pretty.encode("utf-8"))
    if fingerprint == MAINLINE_FINGERPRINT or file_sha == parent.sha256:
        raise B3IdentityError("B3 contract digests must differ from mainline")
    return R008B3Contract(
        path=contract_path,
        raw=document,
        sha256=file_sha,
        campaign_fingerprint=fingerprint,
        runtime_manifest=parent.runtime_manifest,
        schedule=schedule,
        parent_r006=parent,
    )


__all__ = [
    "B3_CONTRACT_PATH",
    "B3IdentityError",
    "R008B3Contract",
    "build_b3_contract_document",
    "compute_b3_campaign_fingerprint",
    "load_b3_contract",
]
