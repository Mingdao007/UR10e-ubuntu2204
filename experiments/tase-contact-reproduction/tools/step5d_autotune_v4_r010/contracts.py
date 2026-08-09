"""Pure R010 build/validate/load and explicit failure-atomic persistence."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .behavior import R010_PROGRAM, R010_RUNTIME_PROTOCOL
from .identity import (
    BEHAVIOR_VERSION,
    R010_LINEAGE,
    BehaviorManifest,
    ReleaseIdentity,
    build_release_identity,
    canonical_bytes,
    require_digest,
    sha256_bytes,
    validate_behavior_manifest,
    validate_release_identity,
)


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_SCHEMA = "step5d.autotune-v4/r010-release-contract-v1"
DEFAULT_CONTRACT_PATH = ROOT / "config/step5d/autotune_v4_r010.json"
DEFAULT_IDENTITY_PATH = ROOT / "config/step5d/autotune_v4_r010.release-identity.json"
OFFLINE_BOUNDARY = {
    "network": False,
    "controller_upload": False,
    "controller_readback": False,
    "dashboard": False,
    "load": False,
    "play": False,
    "bridge": False,
    "motion": False,
    "contact": False,
    "zero": False,
    "tare": False,
    "current_pointer_switch": False,
    "live_evidence": False,
}


class R010ContractError(RuntimeError):
    """R010 contract lifecycle validation failed."""


def contract_bytes(document: Mapping[str, Any]) -> bytes:
    return canonical_bytes(document) + b"\n"


def contract_sha256(document: Mapping[str, Any]) -> str:
    return sha256_bytes(contract_bytes(document))


def identity_bytes(identity: ReleaseIdentity | Mapping[str, Any]) -> bytes:
    typed = identity if isinstance(identity, ReleaseIdentity) else validate_release_identity(identity)
    return canonical_bytes(typed.as_dict()) + b"\n"


def _contract_document(
    behavior_manifest: BehaviorManifest,
    controller_triplet_sha256: Mapping[str, str],
) -> dict[str, Any]:
    triplet = {
        key: require_digest(controller_triplet_sha256.get(key), f"controller {key}")
        for key in ("script", "txt", "urp")
    }
    return {
        "schema": CONTRACT_SCHEMA,
        "version": BEHAVIOR_VERSION,
        "program": R010_PROGRAM,
        "lineage": R010_LINEAGE,
        "revision": 10,
        "status": "offline_candidate_live_blocked",
        "behavior_manifest": behavior_manifest.as_dict(),
        "behavior_manifest_sha256": behavior_manifest.behavior_manifest_sha256,
        "campaign_fingerprint": behavior_manifest.campaign_fingerprint,
        "source_closure_sha256": behavior_manifest.source_closure.sha256,
        "controller_triplet_sha256": triplet,
        "runtime_protocol": R010_RUNTIME_PROTOCOL,
        "offline_boundary": dict(OFFLINE_BOUNDARY),
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
        object.__setattr__(self, "raw", MappingProxyType(dict(self.raw)))

    @property
    def contract_document(self) -> dict[str, Any]:
        return json.loads(canonical_bytes(self.raw).decode("utf-8"))

    @property
    def campaign_fingerprint(self) -> str:
        return self.release_identity.campaign_fingerprint

    @property
    def release_identity_sha256(self) -> str:
        return self.release_identity.release_identity_sha256


def _validate_document(
    document: Mapping[str, Any],
    *,
    expected_sha256: str | None = None,
) -> tuple[BehaviorManifest, dict[str, str], str]:
    required = {
        "schema",
        "version",
        "program",
        "lineage",
        "revision",
        "status",
        "behavior_manifest",
        "behavior_manifest_sha256",
        "campaign_fingerprint",
        "source_closure_sha256",
        "controller_triplet_sha256",
        "runtime_protocol",
        "offline_boundary",
    }
    if not isinstance(document, Mapping) or set(document) != required:
        raise R010ContractError("R010 contract fields differ")
    if (
        document.get("schema") != CONTRACT_SCHEMA
        or document.get("version") != BEHAVIOR_VERSION
        or document.get("program") != R010_PROGRAM
        or document.get("lineage") != R010_LINEAGE
        or document.get("revision") != 10
        or document.get("status") != "offline_candidate_live_blocked"
        or document.get("runtime_protocol") != R010_RUNTIME_PROTOCOL
    ):
        raise R010ContractError("R010 contract identity differs")
    if document.get("offline_boundary") != OFFLINE_BOUNDARY:
        raise R010ContractError("R010 contract is not fail-closed offline")
    try:
        manifest = validate_behavior_manifest(document["behavior_manifest"])
        if document["behavior_manifest_sha256"] != manifest.behavior_manifest_sha256:
            raise R010ContractError("R010 behavior manifest digest differs")
        if document["campaign_fingerprint"] != manifest.campaign_fingerprint:
            raise R010ContractError("R010 campaign fingerprint differs")
        if document["source_closure_sha256"] != manifest.source_closure.sha256:
            raise R010ContractError("R010 source closure digest differs")
        triplet = {
            key: require_digest(document["controller_triplet_sha256"].get(key), f"controller {key}")
            for key in ("script", "txt", "urp")
        }
        if set(document["controller_triplet_sha256"]) != set(triplet):
            raise R010ContractError("R010 controller triplet fields differ")
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, R010ContractError):
            raise
        raise R010ContractError(str(exc)) from exc
    digest = contract_sha256(document)
    if expected_sha256 is not None and digest != require_digest(expected_sha256, "expected contract"):
        raise R010ContractError("R010 contract file digest differs")
    return manifest, triplet, digest


def build_contract(
    *,
    behavior_manifest: BehaviorManifest,
    controller_triplet_sha256: Mapping[str, str],
) -> Contract:
    if not isinstance(behavior_manifest, BehaviorManifest):
        raise R010ContractError("R010 contract requires a typed behavior manifest")
    document = _contract_document(behavior_manifest, controller_triplet_sha256)
    manifest, triplet, digest = _validate_document(document)
    identity = build_release_identity(
        behavior_manifest=manifest,
        final_contract_sha256=digest,
        controller_triplet_sha256=triplet,
    )
    return Contract(document, digest, manifest, identity)


def validate_contract(value: Contract | Mapping[str, Any], *, identity: ReleaseIdentity | Mapping[str, Any] | None = None) -> Contract:
    if isinstance(value, Contract):
        document = value.contract_document
        identity_value: ReleaseIdentity | Mapping[str, Any] = value.release_identity if identity is None else identity
        path = value.path
        identity_path = value.identity_path
    else:
        document = dict(value)
        if identity is None:
            raise R010ContractError("R010 contract validation requires its release identity sidecar")
        identity_value = identity
        path = None
        identity_path = None
    manifest, triplet, digest = _validate_document(document)
    typed_identity = identity_value if isinstance(identity_value, ReleaseIdentity) else validate_release_identity(identity_value)
    expected = build_release_identity(
        behavior_manifest=manifest,
        final_contract_sha256=digest,
        controller_triplet_sha256=triplet,
    )
    if typed_identity.as_dict() != expected.as_dict():
        raise R010ContractError("R010 release identity differs from contract closure")
    return Contract(document, digest, manifest, typed_identity, path, identity_path)


def _strict_object(path: Path, role: str) -> dict[str, Any]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise R010ContractError(f"{role} must be a regular file: {path}")

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise R010ContractError(f"{role} repeats key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=unique_pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(
                R010ContractError(f"{role} contains {token}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise R010ContractError(f"{role} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise R010ContractError(f"{role} must be an object")
    return value


def load_contract(
    path: Path = DEFAULT_CONTRACT_PATH,
    *,
    identity_path: Path = DEFAULT_IDENTITY_PATH,
) -> Contract:
    """Cold-load and validate without modifying either input file."""

    path = Path(path)
    identity_path = Path(identity_path)
    document = _strict_object(path, "R010 contract")
    identity = _strict_object(identity_path, "R010 release identity")
    bundle = validate_contract(document, identity=identity)
    return Contract(
        bundle.raw,
        bundle.sha256,
        bundle.behavior_manifest,
        bundle.release_identity,
        path,
        identity_path,
    )


def _stage_bytes(path: Path, encoded: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise R010ContractError(f"unsafe R010 persist target: {path}")
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


def persist_contract(
    bundle: Contract,
    *,
    path: Path = DEFAULT_CONTRACT_PATH,
    identity_path: Path = DEFAULT_IDENTITY_PATH,
) -> Contract:
    """Explicit two-file persist with rollback if either replacement fails."""

    validated = validate_contract(bundle)
    path = Path(path)
    identity_path = Path(identity_path)
    if path == identity_path:
        raise R010ContractError("contract and identity paths must differ")
    previous = {
        path: path.read_bytes() if path.exists() and path.is_file() and not path.is_symlink() else None,
        identity_path: identity_path.read_bytes()
        if identity_path.exists() and identity_path.is_file() and not identity_path.is_symlink()
        else None,
    }
    staged = {
        path: _stage_bytes(path, contract_bytes(validated.raw)),
        identity_path: _stage_bytes(identity_path, identity_bytes(validated.release_identity)),
    }
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
                    rollback = _stage_bytes(target, old)
                    os.replace(rollback, target)
            except OSError as rollback_exc:
                rollback_errors.append(f"{target}: {rollback_exc}")
        detail = f"; rollback failed: {'; '.join(rollback_errors)}" if rollback_errors else ""
        raise R010ContractError(f"R010 failure-atomic persist failed{detail}") from exc
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
    return load_contract(path, identity_path=identity_path)


__all__ = [
    "CONTRACT_SCHEMA",
    "Contract",
    "DEFAULT_CONTRACT_PATH",
    "DEFAULT_IDENTITY_PATH",
    "OFFLINE_BOUNDARY",
    "R010ContractError",
    "build_contract",
    "contract_bytes",
    "contract_sha256",
    "identity_bytes",
    "load_contract",
    "persist_contract",
    "validate_contract",
]
