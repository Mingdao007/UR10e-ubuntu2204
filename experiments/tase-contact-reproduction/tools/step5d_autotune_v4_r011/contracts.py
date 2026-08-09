"""Pure R011 contract lifecycle with explicit failure-atomic persistence."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .behavior import R011_LINEAGE, R011_PROGRAM, R011_RUNTIME_PROTOCOL
from .common import R011ValueError, canonical_bytes, freeze_tree, require_digest, sha256_bytes, strict_json_object
from .identity import (
    BEHAVIOR_VERSION, BehaviorManifest, ReleaseIdentity, build_release_identity,
    validate_behavior_manifest, validate_release_identity,
)


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_SCHEMA = "step5d.autotune-v4/r011-release-contract-v1"
DEFAULT_CONTRACT_PATH = ROOT / "config/step5d/autotune_v4_r011.json"
DEFAULT_IDENTITY_PATH = ROOT / "config/step5d/autotune_v4_r011.release-identity.json"
OFFLINE_BOUNDARY = {
    "network": False, "controller_upload": False, "controller_readback": False,
    "dashboard": False, "load": False, "play": False, "bridge": False,
    "motion": False, "contact": False, "sensor_write": False,
    "current_pointer_switch": False, "live_process": False, "live_evidence": False,
}


class R011ContractError(RuntimeError):
    """R011 contract lifecycle validation failed."""


def contract_bytes(document: Mapping[str, Any]) -> bytes:
    return canonical_bytes(document) + b"\n"


def contract_sha256(document: Mapping[str, Any]) -> str:
    return sha256_bytes(contract_bytes(document))


def identity_bytes(identity: ReleaseIdentity | Mapping[str, Any]) -> bytes:
    typed = identity if isinstance(identity, ReleaseIdentity) else validate_release_identity(identity)
    return canonical_bytes(typed.as_dict()) + b"\n"


def _document(manifest: BehaviorManifest, triplet: Mapping[str, str]) -> dict[str, Any]:
    controller = {key: require_digest(triplet.get(key), f"R011 controller {key}") for key in ("script", "txt", "urp")}
    return {
        "schema": CONTRACT_SCHEMA,
        "version": BEHAVIOR_VERSION,
        "program": R011_PROGRAM,
        "lineage": R011_LINEAGE,
        "revision": 11,
        "status": "offline_candidate_live_blocked",
        "behavior_manifest": manifest.as_dict(),
        "behavior_manifest_sha256": manifest.behavior_manifest_sha256,
        "campaign_fingerprint": manifest.campaign_fingerprint,
        "source_closure_sha256": manifest.source_closure.sha256,
        "controller_triplet_sha256": controller,
        "controller_triplet_provenance": manifest.raw["controller_triplet_provenance"],
        "runtime_protocol": R011_RUNTIME_PROTOCOL,
        "offline_boundary": dict(OFFLINE_BOUNDARY),
        "persist_policy": "explicit_failure_atomic_only",
        "resume_policy": {"r008": False, "r009": False, "r010": False, "r011_only_exact_identity": True},
    }


@dataclass(frozen=True)
class Contract:
    raw: Mapping[str, Any]
    sha256: str
    behavior_manifest: BehaviorManifest
    release_identity: ReleaseIdentity
    path: Path | None = None
    identity_path: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw", freeze_tree(dict(self.raw)))

    @property
    def contract_document(self) -> dict[str, Any]:
        return json.loads(canonical_bytes(self.raw).decode("utf-8"))

    @property
    def campaign_fingerprint(self) -> str:
        return self.release_identity.campaign_fingerprint

    @property
    def release_identity_sha256(self) -> str:
        return self.release_identity.release_identity_sha256


def _validate_document(document: Mapping[str, Any], expected_sha256: str | None = None) -> tuple[BehaviorManifest, dict[str, str], str]:
    required = {
        "schema", "version", "program", "lineage", "revision", "status", "behavior_manifest",
        "behavior_manifest_sha256", "campaign_fingerprint", "source_closure_sha256",
        "controller_triplet_sha256", "runtime_protocol", "offline_boundary", "persist_policy", "resume_policy",
        "controller_triplet_provenance",
    }
    if not isinstance(document, Mapping) or set(document) != required:
        raise R011ContractError("R011 contract fields differ")
    if document.get("schema") != CONTRACT_SCHEMA or document.get("version") != BEHAVIOR_VERSION or document.get("program") != R011_PROGRAM or document.get("lineage") != R011_LINEAGE or document.get("revision") != 11 or document.get("status") != "offline_candidate_live_blocked" or document.get("runtime_protocol") != R011_RUNTIME_PROTOCOL:
        raise R011ContractError("R011 contract identity differs")
    if document.get("offline_boundary") != OFFLINE_BOUNDARY or document.get("persist_policy") != "explicit_failure_atomic_only" or document.get("resume_policy") != {"r008": False, "r009": False, "r010": False, "r011_only_exact_identity": True}:
        raise R011ContractError("R011 contract boundary differs")
    manifest = validate_behavior_manifest(document["behavior_manifest"])
    if document["behavior_manifest_sha256"] != manifest.behavior_manifest_sha256 or document["campaign_fingerprint"] != manifest.campaign_fingerprint or document["source_closure_sha256"] != manifest.source_closure.sha256:
        raise R011ContractError("R011 contract manifest closure differs")
    triplet = document["controller_triplet_sha256"]
    if not isinstance(triplet, Mapping) or set(triplet) != {"script", "txt", "urp"}:
        raise R011ContractError("R011 controller triplet fields differ")
    typed_triplet = {key: require_digest(triplet[key], f"R011 controller {key}") for key in triplet}
    if document["controller_triplet_provenance"] != manifest.raw["controller_triplet_provenance"]:
        raise R011ContractError("R011 controller triplet provenance differs")
    computed = contract_sha256(document)
    if expected_sha256 is not None and computed != require_digest(expected_sha256, "expected R011 contract"):
        raise R011ContractError("R011 contract digest differs")
    return manifest, typed_triplet, computed


def build_contract(*, behavior_manifest: BehaviorManifest, controller_triplet_sha256: Mapping[str, str]) -> Contract:
    if not isinstance(behavior_manifest, BehaviorManifest):
        raise R011ContractError("R011 contract requires a typed behavior manifest")
    document = _document(behavior_manifest, controller_triplet_sha256)
    manifest, triplet, contract_digest = _validate_document(document)
    identity = build_release_identity(behavior_manifest=manifest, final_contract_sha256=contract_digest, controller_triplet_sha256=triplet)
    return Contract(document, contract_digest, manifest, identity)


def validate_contract(value: Contract | Mapping[str, Any], *, identity: ReleaseIdentity | Mapping[str, Any] | None = None) -> Contract:
    if isinstance(value, Contract):
        document = value.contract_document
        identity_value = value.release_identity if identity is None else identity
        path, identity_path = value.path, value.identity_path
    else:
        document = dict(value)
        if identity is None:
            raise R011ContractError("R011 contract validation requires release identity")
        identity_value = identity
        path = identity_path = None
    manifest, triplet, contract_digest = _validate_document(document)
    typed_identity = identity_value if isinstance(identity_value, ReleaseIdentity) else validate_release_identity(identity_value)
    expected = build_release_identity(behavior_manifest=manifest, final_contract_sha256=contract_digest, controller_triplet_sha256=triplet)
    if typed_identity.as_dict() != expected.as_dict():
        raise R011ContractError("R011 release identity differs from contract closure")
    return Contract(document, contract_digest, manifest, typed_identity, path, identity_path)


def load_contract(path: Path = DEFAULT_CONTRACT_PATH, *, identity_path: Path = DEFAULT_IDENTITY_PATH) -> Contract:
    """Cold-load only: this function never writes, touches mtime, or promotes pointers."""

    path, identity_path = Path(path), Path(identity_path)
    document = strict_json_object(path, "R011 contract")
    identity = strict_json_object(identity_path, "R011 release identity")
    bundle = validate_contract(document, identity=identity)
    return Contract(bundle.raw, bundle.sha256, bundle.behavior_manifest, bundle.release_identity, path, identity_path)


def _stage(path: Path, encoded: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise R011ContractError(f"unsafe R011 persist target: {path}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


def persist_contract(bundle: Contract, *, path: Path = DEFAULT_CONTRACT_PATH, identity_path: Path = DEFAULT_IDENTITY_PATH) -> Contract:
    """Explicit two-file commit with rollback if either replace fails."""

    validated = validate_contract(bundle)
    path, identity_path = Path(path), Path(identity_path)
    if path == identity_path:
        raise R011ContractError("contract and identity paths must differ")
    previous = {target: target.read_bytes() if target.exists() and target.is_file() and not target.is_symlink() else None for target in (path, identity_path)}
    staged = {path: _stage(path, contract_bytes(validated.raw)), identity_path: _stage(identity_path, identity_bytes(validated.release_identity))}
    replaced: list[Path] = []
    try:
        for target in (path, identity_path):
            os.replace(staged[target], target)
            replaced.append(target)
        for directory in {path.parent, identity_path.parent}:
            descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except BaseException as exc:
        rollback_errors: list[str] = []
        for target in reversed(replaced):
            try:
                old = previous[target]
                if old is None:
                    target.unlink(missing_ok=True)
                else:
                    rollback = _stage(target, old)
                    os.replace(rollback, target)
            except OSError as rollback_exc:
                rollback_errors.append(f"{target}: {rollback_exc}")
        suffix = f"; rollback failed: {rollback_errors}" if rollback_errors else ""
        raise R011ContractError(f"R011 failure-atomic persist failed{suffix}") from exc
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
    return load_contract(path, identity_path=identity_path)


__all__ = [
    "CONTRACT_SCHEMA", "Contract", "DEFAULT_CONTRACT_PATH", "DEFAULT_IDENTITY_PATH", "OFFLINE_BOUNDARY",
    "R011ContractError", "build_contract", "contract_bytes", "contract_sha256", "identity_bytes",
    "load_contract", "persist_contract", "validate_contract",
]
