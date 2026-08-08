"""Pure R009 contract lifecycle and release-identity binding.

The lifecycle has three deliberately separate operations:

* ``build_contract`` constructs an in-memory release bundle only;
* ``validate_contract`` and ``load_contract`` only validate/read bytes;
* ``persist_contract`` is the sole operation that writes contract or identity
  files, using the repository's fsync + atomic-replace convention.

The release identity is stored as a sidecar rather than embedded in the final
contract payload.  ``final_contract_sha256`` therefore has a stable meaning
and cannot participate in a self-referential fixed point.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .identity import (
    DEFAULT_CONTROLLER_TRIPLET_SHA256,
    R009BehaviorManifest,
    R009IdentityError,
    R009ReleaseIdentity,
    R009_BEHAVIOR_VERSION,
    R009_LINEAGE,
    R009_PROGRAM,
    R009_RUNTIME_PROTOCOL,
    build_release_identity,
    canonical_bytes,
    require_digest,
    sha256_bytes,
    _freeze_json,
    _thaw_json,
    validate_behavior_manifest,
    validate_release_identity,
)


CONTRACT_SCHEMA = "step5d.autotune-v4/r009-release-contract-v1"
CONTRACT_VERSION = R009_BEHAVIOR_VERSION
DEFAULT_CONTRACT_PATH = Path(__file__).resolve().parents[2] / "config/step5d/autotune_v4_r009.json"

OFFLINE_BOUNDARY = {
    "arm": False,
    "bridge": False,
    "contact": False,
    "controller_readback": False,
    "controller_upload": False,
    "dashboard": False,
    "live_evidence": False,
    "load": False,
    "motion": False,
    "network": False,
    "play": False,
    "tare": False,
    "zero": False,
}


class R009ContractError(RuntimeError):
    """A pure R009 contract or sidecar identity is invalid."""


def contract_bytes(document: Mapping[str, Any]) -> bytes:
    """Canonical persisted bytes for the final contract payload."""

    return canonical_bytes(document) + b"\n"


def contract_sha256(document: Mapping[str, Any]) -> str:
    return sha256_bytes(contract_bytes(document))


def identity_bytes(identity: R009ReleaseIdentity | Mapping[str, Any]) -> bytes:
    document = (
        identity.as_dict()
        if isinstance(identity, R009ReleaseIdentity)
        else validate_release_identity(identity).as_dict()
    )
    return canonical_bytes(document) + b"\n"


def identity_file_sha256(identity: R009ReleaseIdentity | Mapping[str, Any]) -> str:
    return sha256_bytes(identity_bytes(identity))


def _strict_object(encoded: bytes, role: str) -> dict[str, Any]:
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise R009ContractError(f"{role} repeats JSON key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=unique_pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(
                R009ContractError(f"{role} contains non-finite JSON constant {token}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise R009ContractError(f"{role} is not strict JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise R009ContractError(f"{role} must be a JSON object")
    return value


def _regular_file(path: Path, role: str) -> Path:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise R009ContractError(f"{role} must be a regular file: {path}")
    return path


def _contract_document(
    *,
    behavior_manifest: R009BehaviorManifest,
    controller_triplet_sha256: Mapping[str, str],
) -> dict[str, Any]:
    # Importing this type through its constructor keeps triplet validation in
    # the identity primitive while preserving a plain JSON payload here.
    from .identity import ControllerTriplet

    triplet = ControllerTriplet.from_mapping(controller_triplet_sha256)
    return {
        "schema": CONTRACT_SCHEMA,
        "version": CONTRACT_VERSION,
        "program": R009_PROGRAM,
        "lineage": R009_LINEAGE,
        "revision": 9,
        "status": "offline_candidate_live_blocked",
        "behavior_manifest": behavior_manifest.as_dict(),
        "behavior_manifest_sha256": behavior_manifest.behavior_manifest_sha256,
        "campaign_fingerprint": behavior_manifest.campaign_fingerprint,
        "source_closure_sha256": behavior_manifest.source_set.sha256,
        "controller_triplet_sha256": triplet.as_dict(),
        "runtime_protocol": behavior_manifest.runtime_protocol,
        "executable_behavior_config": behavior_manifest.executable_behavior_config.as_dict(),
        "offline_boundary": dict(OFFLINE_BOUNDARY),
    }


def build_contract_document(
    *,
    behavior_manifest: R009BehaviorManifest,
    controller_triplet_sha256: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Purely build the final contract payload; never write it."""

    if not isinstance(behavior_manifest, R009BehaviorManifest):
        raise R009ContractError("behavior_manifest must be the typed R009 manifest")
    triplet = (
        DEFAULT_CONTROLLER_TRIPLET_SHA256
        if controller_triplet_sha256 is None
        else controller_triplet_sha256
    )
    try:
        return _contract_document(
            behavior_manifest=behavior_manifest,
            controller_triplet_sha256=triplet,
        )
    except (R009IdentityError, TypeError, ValueError) as exc:
        raise R009ContractError(str(exc)) from exc


def _validate_document(
    document: Mapping[str, Any],
    *,
    expected_sha256: str | None = None,
) -> tuple[R009BehaviorManifest, dict[str, str], str]:
    if not isinstance(document, Mapping):
        raise R009ContractError("R009 contract must be an object")
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
        "executable_behavior_config",
        "offline_boundary",
    }
    if set(document) != required:
        raise R009ContractError("R009 contract fields differ")
    if (
        document.get("schema") != CONTRACT_SCHEMA
        or document.get("version") != CONTRACT_VERSION
        or document.get("program") != R009_PROGRAM
        or document.get("lineage") != R009_LINEAGE
        or document.get("revision") != 9
        or document.get("status") != "offline_candidate_live_blocked"
    ):
        raise R009ContractError("R009 contract program/lineage/revision differs")
    boundary = document.get("offline_boundary")
    if not isinstance(boundary, Mapping) or dict(boundary) != OFFLINE_BOUNDARY:
        raise R009ContractError("R009 offline boundary is not fail-closed")

    try:
        behavior = validate_behavior_manifest(document["behavior_manifest"])
        require_digest(
            document["behavior_manifest_sha256"], "R009 contract behavior manifest sha256"
        )
        if document["behavior_manifest_sha256"] != behavior.behavior_manifest_sha256:
            raise R009ContractError("R009 contract behavior manifest digest differs")
        if document["campaign_fingerprint"] != behavior.campaign_fingerprint:
            raise R009ContractError("R009 contract campaign fingerprint differs")
        if document["source_closure_sha256"] != behavior.source_set.sha256:
            raise R009ContractError("R009 contract source closure digest differs")
        from .identity import ControllerTriplet, ExecutableBehaviorConfig

        triplet = ControllerTriplet.from_mapping(document["controller_triplet_sha256"])
        if document["runtime_protocol"] != behavior.runtime_protocol:
            raise R009ContractError("R009 contract runtime protocol differs")
        config = ExecutableBehaviorConfig.from_mapping(document["executable_behavior_config"])
        if config.as_dict() != behavior.executable_behavior_config.as_dict():
            raise R009ContractError("R009 contract executable behavior differs")
    except (R009IdentityError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, R009ContractError):
            raise
        raise R009ContractError(str(exc)) from exc

    encoded_sha = contract_sha256(document)
    if expected_sha256 is not None and encoded_sha != require_digest(
        expected_sha256, "expected R009 contract sha256"
    ):
        raise R009ContractError("R009 contract file digest differs")
    return behavior, triplet.as_dict(), encoded_sha


@dataclass(frozen=True)
class R009Contract:
    """Validated contract payload plus its independently computed identity."""

    raw: Mapping[str, Any]
    sha256: str
    behavior_manifest: R009BehaviorManifest
    release_identity: R009ReleaseIdentity
    path: Path | None = None
    identity_path: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw", _freeze_json(self.raw))

    @property
    def campaign_fingerprint(self) -> str:
        return self.release_identity.campaign_fingerprint

    @property
    def release_identity_sha256(self) -> str:
        return self.release_identity.release_identity_sha256

    @property
    def contract_document(self) -> dict[str, Any]:
        return _thaw_json(self.raw)


def _bundle_from_document(
    document: Mapping[str, Any],
    *,
    path: Path | None = None,
    identity: R009ReleaseIdentity | None = None,
    identity_path: Path | None = None,
    expected_sha256: str | None = None,
) -> R009Contract:
    behavior, triplet, computed_sha = _validate_document(
        document, expected_sha256=expected_sha256
    )
    derived = build_release_identity(
        behavior_manifest=behavior,
        final_contract_sha256=computed_sha,
        controller_triplet_sha256=triplet,
    )
    if identity is not None:
        try:
            identity.validate_against(behavior, final_contract_sha256=computed_sha)
        except R009IdentityError as exc:
            raise R009ContractError(str(exc)) from exc
        if identity.as_dict() != derived.as_dict():
            raise R009ContractError("R009 release identity differs from contract payload")
    return R009Contract(
        raw=dict(document),
        sha256=computed_sha,
        behavior_manifest=behavior,
        release_identity=derived,
        path=path,
        identity_path=identity_path,
    )


def build_contract(
    *,
    behavior_manifest: R009BehaviorManifest,
    controller_triplet_sha256: Mapping[str, str] | None = None,
) -> R009Contract:
    """Purely build a complete in-memory R009 contract/identity bundle."""

    document = build_contract_document(
        behavior_manifest=behavior_manifest,
        controller_triplet_sha256=controller_triplet_sha256,
    )
    return _bundle_from_document(document)


def validate_contract(
    document: Mapping[str, Any] | R009Contract,
    *,
    expected_sha256: str | None = None,
    release_identity: R009ReleaseIdentity | Mapping[str, Any] | None = None,
) -> R009Contract:
    """Pure validation; it never creates or modifies an on-disk file."""

    if isinstance(document, R009Contract):
        raw = document.raw
        if expected_sha256 is None:
            expected_sha256 = document.sha256
    else:
        raw = document
    identity = None
    if release_identity is not None:
        identity = (
            release_identity
            if isinstance(release_identity, R009ReleaseIdentity)
            else validate_release_identity(release_identity)
        )
    return _bundle_from_document(raw, expected_sha256=expected_sha256, identity=identity)


def _default_identity_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.release-identity.json")


def load_release_identity(path: Path) -> R009ReleaseIdentity:
    path = _regular_file(Path(path), "R009 release identity")
    identity = validate_release_identity(_strict_object(path.read_bytes(), "R009 release identity"))
    return identity


def load_contract(
    path: Path = DEFAULT_CONTRACT_PATH,
    *,
    identity_path: Path | None = None,
    expected_release_identity: R009ReleaseIdentity | Mapping[str, Any] | None = None,
) -> R009Contract:
    """Pure read/validate operation; no fallback generation or persistence."""

    path = _regular_file(Path(path), "R009 contract")
    encoded = path.read_bytes()
    document = _strict_object(encoded, "R009 contract")
    sidecar_path = _default_identity_path(path) if identity_path is None else Path(identity_path)
    identity: R009ReleaseIdentity | None = None
    if sidecar_path.exists():
        identity = load_release_identity(sidecar_path)
    elif identity_path is not None:
        raise R009ContractError(f"R009 release identity sidecar is missing: {sidecar_path}")
    if expected_release_identity is not None:
        expected = (
            expected_release_identity
            if isinstance(expected_release_identity, R009ReleaseIdentity)
            else validate_release_identity(expected_release_identity)
        )
        if identity is not None and identity.as_dict() != expected.as_dict():
            raise R009ContractError("expected R009 release identity differs from sidecar")
        identity = expected
    return _bundle_from_document(
        document,
        path=path,
        identity=identity,
        identity_path=sidecar_path if sidecar_path.exists() else None,
        expected_sha256=sha256_bytes(encoded),
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_bytes(path: Path, encoded: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise R009ContractError(f"refusing unsafe R009 persist path: {path}")
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


def persist_contract(
    bundle: R009Contract,
    path: Path = DEFAULT_CONTRACT_PATH,
    *,
    identity_path: Path | None = None,
) -> R009Contract:
    """The sole R009 contract/identity writer, using atomic fsync replacement."""

    if not isinstance(bundle, R009Contract):
        raise R009ContractError("persist_contract requires a R009Contract bundle")
    validated = validate_contract(bundle)
    path = Path(path)
    sidecar_path = _default_identity_path(path) if identity_path is None else Path(identity_path)
    _atomic_bytes(path, contract_bytes(validated.raw))
    _atomic_bytes(sidecar_path, identity_bytes(validated.release_identity))
    return R009Contract(
        raw=validated.raw,
        sha256=validated.sha256,
        behavior_manifest=validated.behavior_manifest,
        release_identity=validated.release_identity,
        path=path,
        identity_path=sidecar_path,
    )


# Explicit aliases make the lifecycle seam easy to discover without creating
# a second implementation or writer.
build = build_contract
validate = validate_contract
persist = persist_contract


__all__ = [
    "CONTRACT_SCHEMA",
    "CONTRACT_VERSION",
    "DEFAULT_CONTRACT_PATH",
    "OFFLINE_BOUNDARY",
    "R009Contract",
    "R009ContractError",
    "build",
    "build_contract",
    "build_contract_document",
    "contract_bytes",
    "contract_sha256",
    "identity_bytes",
    "identity_file_sha256",
    "load_contract",
    "load_release_identity",
    "persist",
    "persist_contract",
    "validate",
    "validate_contract",
]
