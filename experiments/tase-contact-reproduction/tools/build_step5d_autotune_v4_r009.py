#!/usr/bin/env python3
"""Build an R009 TP triplet into an explicitly supplied offline directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from step5d_autotune_v4_r009.contracts import (
    DEFAULT_CONTRACT_PATH,
    R009Contract,
    build_contract,
    canonical_bytes,
    contract_bytes,
    identity_bytes,
    load_contract,
    validate_contract,
)
from step5d_autotune_v4_r009.identity import (
    R009BehaviorManifest,
    R009ReleaseIdentity,
    R009SourceSet,
    build_behavior_manifest_from_r006,
    sha256_file,
    sha256_bytes,
    validate_behavior_manifest,
    validate_release_identity,
)
from step5d_autotune_v4_r009.ledger import (
    GENESIS_SHA256,
    HEADER_RECORD_TYPE,
    LEDGER_SCHEMA,
)
from step5d_autotune_v4_r009.tp import (
    CONTROLLER_DIRECTORY,
    R009_STAMP,
    R009TPError,
    build_triplet,
    numeric_sanity,
    validate_triplet,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "programs/step5/step5d"
DEFAULT_BEHAVIOR_MANIFEST_PATH = ROOT / "config/step5d/autotune_v4_r009.behavior-manifest.json"
DEFAULT_CLOSURE_PATH = ROOT / "config/step5d/autotune_v4_r009_offline_closure.json"
DEFAULT_IDENTITY_ALIAS_PATH = ROOT / "config/step5d/r009_release_identity.json"
R009_CLOSURE_SCHEMA = "step5d.autotune-v4/r009-offline-closure-v1"
R009_OBSERVATION_BINDING_SCHEMA = "step5d.autotune-v4/r009-observation-release-binding-v1"
ARTIFACT_SUFFIXES = (
    ".script",
    ".txt",
    ".urp",
    ".numeric-sanity.json",
    ".deploy-manifest.json",
)
ARTIFACT_KEYS = tuple(
    suffix.lstrip(".").replace(".", "_") for suffix in ARTIFACT_SUFFIXES
)
TRIPLET_SUFFIXES = (".script", ".txt", ".urp")


class R009BuilderError(RuntimeError):
    """The offline R009 builder cannot prove a complete identity closure."""


class R009RollbackError(R009BuilderError):
    """A failed commit whose rollback could not restore every target."""

    def __init__(
        self,
        original_error: BaseException,
        failures: Sequence[tuple[Path, OSError]],
    ) -> None:
        self.original_error = original_error
        self.unrecovered_targets = tuple(path for path, _ in failures)
        details = "; ".join(
            f"{path}: {str(error)[:160]}" for path, error in failures[:32]
        )
        if len(failures) > 32:
            details += f"; ... {len(failures) - 32} more"
        super().__init__(
            "R009 atomic persist commit failed and rollback was incomplete; "
            f"unrecovered targets: {details}"
        )


def _manifest_from_input(identity_input: Any) -> R009BehaviorManifest:
    if isinstance(identity_input, R009BehaviorManifest):
        return identity_input
    candidate = getattr(identity_input, "behavior_manifest", None)
    if isinstance(candidate, R009BehaviorManifest):
        return candidate
    raise R009BuilderError("R009 builder identity input is not typed")


def _triplet_hashes(paths: Mapping[str, Path]) -> dict[str, str]:
    try:
        return {
            suffix: hashlib.sha256(Path(paths[f".{suffix}"]).read_bytes()).hexdigest()
            for suffix in ("script", "txt", "urp")
        }
    except (KeyError, OSError) as exc:
        raise R009BuilderError("R009 builder did not produce the controller triplet") from exc


def _absolute_path(path: Path) -> Path:
    """Normalize a path lexically without silently following symlinks."""

    path = Path(path)
    return Path(os.path.abspath(path if path.is_absolute() else Path.cwd() / path))


def _reject_lexical_symlink_ancestors(path: Path, role: str) -> Path:
    """Reject symlinks before any resolved-path operation is attempted."""

    absolute = _absolute_path(Path(path))
    current = absolute
    while True:
        if current.is_symlink():
            raise R009BuilderError(f"R009 {role} has a symlink ancestor: {current}")
        parent = current.parent
        if parent == current:
            break
        current = parent
    return absolute


def _safe_target_path(
    release_root: Path,
    path: Path,
    role: str,
    *,
    require_file: bool = False,
) -> Path:
    """Return a release-root path after checking every existing ancestor."""

    root = _reject_lexical_symlink_ancestors(
        Path(release_root), f"{role} release root"
    )
    candidate = _absolute_path(Path(path))
    _reject_lexical_symlink_ancestors(candidate, role)
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise R009BuilderError(f"R009 {role} path escapes release_root") from exc

    resolved_root = root.resolve(strict=False)
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise R009BuilderError(f"R009 {role} path escapes release_root") from exc
    if candidate.exists():
        if candidate.is_symlink() or not candidate.is_file():
            raise R009BuilderError(f"R009 {role} target is not a regular file")
    elif require_file:
        raise R009BuilderError(f"R009 {role} target is missing")
    return candidate


def _relative_to_root(
    release_root: Path,
    path: Path,
    role: str,
    *,
    require_file: bool = False,
) -> str:
    candidate = _safe_target_path(
        release_root,
        path,
        role,
        require_file=require_file,
    )
    return candidate.relative_to(_absolute_path(Path(release_root))).as_posix()


def _safe_existing_file(path: Path, role: str) -> Path:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise R009BuilderError(f"R009 {role} must be a regular file: {path}")
    ancestor = path.parent
    while ancestor != ancestor.parent:
        if ancestor.is_symlink():
            raise R009BuilderError(f"R009 {role} has a symlink ancestor: {path}")
        ancestor = ancestor.parent
    return path


def _read_source_artifacts(
    source_paths: Mapping[str, Path],
) -> dict[str, bytes]:
    if set(source_paths) != set(ARTIFACT_SUFFIXES):
        raise R009BuilderError("R009 persist requires exactly five source artifacts")
    return {
        suffix: _safe_existing_file(path, f"source artifact {suffix}").read_bytes()
        for suffix, path in source_paths.items()
    }


def _validate_source_artifacts(
    source_bytes: Mapping[str, bytes],
    *,
    manifest: R009BehaviorManifest,
    identity: R009ReleaseIdentity,
    stamp: str,
) -> dict[str, str]:
    if set(source_bytes) != set(ARTIFACT_SUFFIXES):
        raise R009BuilderError("R009 source artifact set differs")
    try:
        script = source_bytes[".script"].decode("utf-8")
        txt = source_bytes[".txt"].decode("utf-8")
        triplet_checks = validate_triplet(
            script,
            txt,
            source_bytes[".urp"],
            stamp,
            manifest,
        )
        expected_sanity = {
            **numeric_sanity(script),
            "triplet_checks": triplet_checks,
        }
        sanity = json.loads(source_bytes[".numeric-sanity.json"].decode("utf-8"))
        if sanity != expected_sanity:
            raise R009BuilderError("R009 numeric-sanity artifact semantics differ")
        triplet = {
            suffix.lstrip("."): sha256_bytes(source_bytes[suffix])
            for suffix in TRIPLET_SUFFIXES
        }
        deploy = json.loads(source_bytes[".deploy-manifest.json"].decode("utf-8"))
        expected_deploy = {
            "schema": "step5d.autotune-v4/r009-deploy-manifest-v1",
            "program": manifest.program,
            "controller_directory": CONTROLLER_DIRECTORY,
            "campaign_fingerprint": manifest.campaign_fingerprint,
            "behavior_manifest_sha256": manifest.behavior_manifest_sha256,
            "runtime_protocol": manifest.runtime_protocol,
            "same_basename_triplet": True,
            "timestamp": stamp,
            "artifacts": triplet,
            "controller_upload": False,
            "controller_readback": False,
            "live_evidence": False,
        }
        if deploy != expected_deploy:
            raise R009BuilderError("R009 deploy-manifest artifact semantics differ")
    except (R009TPError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        if isinstance(exc, R009BuilderError):
            raise
        raise R009BuilderError("R009 source artifact semantics differ") from exc
    if triplet != dict(identity.controller_triplet_sha256):
        raise R009BuilderError("R009 source triplet differs from release identity")
    return triplet


def _runtime_protocol_summary(manifest: R009BehaviorManifest) -> dict[str, Any]:
    return {
        "runtime_protocol": manifest.runtime_protocol,
        "raw_codec": manifest.raw_codec,
        "contact_search_schedule": manifest.contact_search_schedule.as_dict(),
        "executable_behavior_config": manifest.executable_behavior_config.as_dict(),
    }


def _ledger_header_identity(identity: R009ReleaseIdentity) -> dict[str, Any]:
    header = {
        "schema": LEDGER_SCHEMA,
        "record_type": HEADER_RECORD_TYPE,
        "release_identity": identity.as_dict(),
        "release_identity_sha256": identity.release_identity_sha256,
        "campaign_fingerprint": identity.campaign_fingerprint,
        "previous_record_sha256": GENESIS_SHA256,
    }
    header["record_sha256"] = sha256_bytes(canonical_bytes(header))
    return header


def _observation_binding(identity: R009ReleaseIdentity) -> dict[str, Any]:
    return {
        "schema": R009_OBSERVATION_BINDING_SCHEMA,
        "release_identity_sha256": identity.release_identity_sha256,
        "campaign_fingerprint": identity.campaign_fingerprint,
    }


def _result(
    *,
    manifest: R009BehaviorManifest,
    bundle: R009Contract,
    triplet: Mapping[str, str],
    paths: Mapping[str, Path] | None,
    stamp: str,
) -> dict[str, object]:
    identity = bundle.release_identity
    return {
        "program": manifest.program,
        "output_dir": str(Path(next(iter(paths.values())).parent)) if paths else None,
        "campaign_fingerprint": manifest.campaign_fingerprint,
        "behavior_manifest_sha256": manifest.behavior_manifest_sha256,
        "behavior_manifest": manifest.as_dict(),
        "source_closure": manifest.source_set.as_dict(),
        "source_closure_sha256": manifest.source_set.sha256,
        "runtime_protocol": manifest.runtime_protocol,
        "stamp": stamp,
        "runtime_protocol_summary": _runtime_protocol_summary(manifest),
        "controller_triplet_sha256": dict(triplet),
        "contract": bundle.contract_document,
        "contract_sha256": bundle.sha256,
        "release_identity": identity.as_dict(),
        "release_identity_sha256": identity.release_identity_sha256,
        "ledger_header_identity": _ledger_header_identity(identity),
        "observation_binding": _observation_binding(identity),
        "artifacts": (
            {key: str(value) for key, value in paths.items()} if paths else {}
        ),
        "controller_upload": False,
        "controller_readback": False,
        "live_evidence": False,
    }


def _bundle_for_manifest(
    manifest: R009BehaviorManifest,
    triplet: Mapping[str, str],
    supplied_contract: Any | None,
) -> R009Contract:
    if supplied_contract is None:
        return build_contract(
            behavior_manifest=manifest,
            controller_triplet_sha256=triplet,
        )
    if not isinstance(supplied_contract, R009Contract):
        raise R009BuilderError("R009 builder contract input is not typed")
    bundle = validate_contract(supplied_contract)
    if bundle.behavior_manifest.behavior_manifest_sha256 != manifest.behavior_manifest_sha256:
        raise R009BuilderError("R009 contract behavior manifest differs from builder input")
    if dict(bundle.raw["controller_triplet_sha256"]) != dict(triplet):
        raise R009BuilderError("R009 contract controller triplet differs from built bytes")
    return bundle


def _build_triplet_idempotent(
    output_dir: Path,
    *,
    behavior_manifest: R009BehaviorManifest,
    stamp: str,
) -> dict[str, Path]:
    """Use the project TP builder while preserving identical target mtimes."""

    output_dir = Path(output_dir)
    with tempfile.TemporaryDirectory(prefix="step5d-r009-triplet-") as temporary:
        generated = build_triplet(
            Path(temporary),
            behavior_manifest=behavior_manifest,
            stamp=stamp,
        )
        targets: dict[str, Path] = {}
        for key, source in generated.items():
            target = output_dir / Path(source).name
            _write_explicit(target, Path(source).read_bytes())
            targets[key] = target
        return targets


def build(
    output_dir: Path,
    *,
    behavior_manifest: R009BehaviorManifest | None = None,
    manifest: R009BehaviorManifest | None = None,
    contract: R009Contract | None = None,
    stamp: str = R009_STAMP,
) -> dict[str, object]:
    """Build one explicit offline R009 triplet and its in-memory identity closure.

    The triplet directory is the only path written by this operation.  The
    contract, behavior manifest, identity sidecars, and closure are written
    only by :func:`persist_release` (or the CLI ``--persist`` switch).
    """

    supplied = [
        value for value in (behavior_manifest, manifest, contract) if value is not None
    ]
    if len(supplied) > 1:
        raise TypeError("R009 builder received multiple identity inputs")
    identity_input = supplied[0] if supplied else build_behavior_manifest_from_r006()
    resolved_manifest = _manifest_from_input(identity_input)
    paths = _build_triplet_idempotent(
        Path(output_dir), behavior_manifest=resolved_manifest, stamp=stamp
    )
    triplet = _triplet_hashes(paths)
    bundle = _bundle_for_manifest(
        resolved_manifest,
        triplet,
        identity_input if isinstance(identity_input, R009Contract) else None,
    )
    return _result(
        manifest=resolved_manifest,
        bundle=bundle,
        triplet=triplet,
        paths=paths,
        stamp=stamp,
    )


def build_release(
    *,
    behavior_manifest: R009BehaviorManifest | None = None,
    manifest: R009BehaviorManifest | None = None,
    contract: R009Contract | None = None,
    stamp: str = R009_STAMP,
) -> dict[str, object]:
    """Build and validate the complete identity closure in a temporary directory.

    This is the pure builder entry point: repository contract/identity bytes
    and the caller's files are untouched.  Use ``persist_release`` to make
    the result durable.
    """

    with tempfile.TemporaryDirectory(prefix="step5d-r009-build-") as temporary:
        result = build(
            Path(temporary),
            behavior_manifest=behavior_manifest,
            manifest=manifest,
            contract=contract,
            stamp=stamp,
        )
        return {
            key: value
            for key, value in result.items()
            if key not in {"artifacts", "output_dir"}
        } | {
            "output_dir": None,
            "artifacts": {
                suffix: {
                    "sha256": digest,
                    "size": len(
                        (Path(result["artifacts"][f".{suffix}"])).read_bytes()
                    ),
                }
                for suffix, digest in result["controller_triplet_sha256"].items()
            }
        }


def build_closure_document(
    result: Mapping[str, Any],
    *,
    artifact_paths: Mapping[str, Path] | None = None,
    contract_path: Path = DEFAULT_CONTRACT_PATH,
    behavior_manifest_path: Path = DEFAULT_BEHAVIOR_MANIFEST_PATH,
    identity_path: Path | None = None,
    release_root: Path = ROOT,
    artifact_bytes: Mapping[str, bytes] | None = None,
) -> dict[str, Any]:
    """Purely derive an R009 closure with paths relative to ``release_root``."""

    identity = validate_release_identity(result["release_identity"])
    manifest = validate_behavior_manifest(result["behavior_manifest"])
    bundle = validate_contract(result["contract"], release_identity=identity)
    paths = artifact_paths
    if paths is None:
        raw_paths = result.get("artifacts")
        if not isinstance(raw_paths, Mapping):
            raise R009BuilderError("R009 closure requires artifact paths")
        paths = {key: Path(value) for key, value in raw_paths.items()}
    if set(paths) != set(ARTIFACT_SUFFIXES):
        raise R009BuilderError("R009 closure requires exactly five artifact paths")
    if artifact_bytes is not None and set(artifact_bytes) != set(ARTIFACT_SUFFIXES):
        raise R009BuilderError("R009 closure artifact bytes differ")
    artifacts: dict[str, dict[str, object]] = {}
    for suffix in ARTIFACT_SUFFIXES:
        path = Path(paths[suffix])
        relative = _relative_to_root(
            release_root,
            path,
            f"artifact {suffix}",
            require_file=artifact_bytes is None,
        )
        encoded = (
            artifact_bytes[suffix]
            if artifact_bytes is not None
            else _safe_existing_file(path, f"artifact {suffix}").read_bytes()
        )
        artifacts[suffix.lstrip(".").replace(".", "_")] = {
            "path": relative,
            "sha256": sha256_bytes(encoded),
        }
    identity_file_digest = sha256_bytes(identity_bytes(identity))
    identity_document_path = (
        Path(identity_path)
        if identity_path is not None
        else Path(contract_path).with_name(f"{Path(contract_path).stem}.release-identity.json")
    )
    ledger_identity = _ledger_header_identity(identity)
    observation_binding = _observation_binding(identity)
    payload: dict[str, Any] = {
        "schema": R009_CLOSURE_SCHEMA,
        "program": manifest.program,
        "lineage": manifest.lineage,
        "campaign_fingerprint": manifest.campaign_fingerprint,
        "behavior_manifest_sha256": manifest.behavior_manifest_sha256,
        "source_closure_sha256": manifest.source_set.sha256,
        "source_closure": manifest.source_set.as_dict(),
        "behavior_manifest": {
            "path": _relative_to_root(
                release_root,
                Path(behavior_manifest_path),
                "behavior manifest",
            ),
            "sha256": manifest.behavior_manifest_sha256,
        },
        "release_contract": {
            "path": _relative_to_root(release_root, Path(contract_path), "contract"),
            "sha256": bundle.sha256,
        },
        "release_identity": {
            "path": _relative_to_root(
                release_root,
                identity_document_path,
                "release identity",
            ),
            "file_sha256": identity_file_digest,
            "release_identity_sha256": identity.release_identity_sha256,
            "value": identity.as_dict(),
        },
        "controller_triplet_sha256": dict(identity.controller_triplet_sha256),
        "runtime_protocol": _runtime_protocol_summary(manifest),
        "artifacts": artifacts,
        "ledger_header_identity": ledger_identity,
        "observation_binding": observation_binding,
        "offline_boundary": {
            "controller_upload": False,
            "controller_readback": False,
            "live_evidence": False,
            "network": False,
        },
        "claim_boundary": "offline source/package/identity closure only; no live acceptance",
    }
    basis = dict(payload)
    payload["content_address"] = {
        "algorithm": "SHA-256",
        "basis": "r009-offline-closure-v1 excludes its own content_address",
        "sha256": sha256_bytes(canonical_bytes(basis)),
    }
    return payload


def _root_relative(root: Path, value: Any, role: str) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise R009BuilderError(f"R009 closure {role} path is not relative")
    return _safe_target_path(Path(root), Path(root) / value, role)


def _source_closure_path(root: Path, relative: str) -> Path:
    """Read a source only from the supplied release root."""

    return _root_relative(root, relative, "source")


def validate_closure_document(
    document: Mapping[str, Any],
    *,
    root: Path = ROOT,
) -> dict[str, Any]:
    """Purely validate every byte and identity binding in an R009 closure."""

    if not isinstance(document, Mapping) or document.get("schema") != R009_CLOSURE_SCHEMA:
        raise R009BuilderError("R009 closure schema differs")
    required = {
        "schema",
        "program",
        "lineage",
        "campaign_fingerprint",
        "behavior_manifest_sha256",
        "source_closure_sha256",
        "source_closure",
        "behavior_manifest",
        "release_contract",
        "release_identity",
        "controller_triplet_sha256",
        "runtime_protocol",
        "artifacts",
        "ledger_header_identity",
        "observation_binding",
        "offline_boundary",
        "claim_boundary",
        "content_address",
    }
    if set(document) != required:
        raise R009BuilderError("R009 closure fields differ")
    root = _reject_lexical_symlink_ancestors(Path(root), "closure root")
    manifest_row = document["behavior_manifest"]
    contract_row = document["release_contract"]
    identity_row = document["release_identity"]
    if not all(isinstance(row, Mapping) for row in (manifest_row, contract_row, identity_row)):
        raise R009BuilderError("R009 closure identity paths are malformed")
    manifest_path = _root_relative(root, manifest_row.get("path"), "behavior manifest")
    contract_path = _root_relative(root, contract_row.get("path"), "contract")
    identity_path = _root_relative(root, identity_row.get("path"), "release identity")
    if manifest_path.is_symlink() or contract_path.is_symlink() or identity_path.is_symlink():
        raise R009BuilderError("R009 closure identity path is a symlink")
    try:
        manifest = validate_behavior_manifest(
            json.loads(manifest_path.read_text(encoding="utf-8"))
        )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise R009BuilderError("R009 behavior manifest cannot be loaded") from exc
    if manifest.behavior_manifest_sha256 != document["behavior_manifest_sha256"]:
        raise R009BuilderError("R009 closure behavior manifest digest differs")
    if manifest_row.get("sha256") != manifest.behavior_manifest_sha256:
        raise R009BuilderError("R009 closure behavior manifest file binding differs")
    source_set = R009SourceSet.from_mapping(document["source_closure"])
    if source_set.sha256 != document["source_closure_sha256"]:
        raise R009BuilderError("R009 closure source digest differs")
    for relative, expected in source_set.files.items():
        path = _source_closure_path(root, relative)
        if path.is_symlink() or not path.is_file() or sha256_file(path) != expected:
            raise R009BuilderError(f"R009 closure source byte digest differs: {relative}")
    try:
        bundle = load_contract(contract_path, identity_path=identity_path)
    except Exception as exc:
        raise R009BuilderError("R009 closure contract/identity cannot be loaded") from exc
    if bundle.campaign_fingerprint != document["campaign_fingerprint"]:
        raise R009BuilderError("R009 closure campaign fingerprint differs")
    if bundle.sha256 != contract_row.get("sha256"):
        raise R009BuilderError("R009 closure contract digest differs")
    identity = validate_release_identity(identity_row.get("value"))
    if identity.as_dict() != bundle.release_identity.as_dict():
        raise R009BuilderError("R009 closure release identity differs")
    if identity.release_identity_sha256 != identity_row.get("release_identity_sha256"):
        raise R009BuilderError("R009 closure release identity digest differs")
    if sha256_file(identity_path) != identity_row.get("file_sha256"):
        raise R009BuilderError("R009 closure release identity file digest differs")
    if dict(document["controller_triplet_sha256"]) != dict(identity.controller_triplet_sha256):
        raise R009BuilderError("R009 closure controller triplet differs")
    if document["runtime_protocol"] != _runtime_protocol_summary(manifest):
        raise R009BuilderError("R009 closure runtime protocol summary differs")
    if document["ledger_header_identity"] != _ledger_header_identity(identity):
        raise R009BuilderError("R009 closure ledger header identity differs")
    if document["observation_binding"] != _observation_binding(identity):
        raise R009BuilderError("R009 closure observation binding differs")
    artifacts = document["artifacts"]
    if not isinstance(artifacts, Mapping) or set(artifacts) != set(ARTIFACT_KEYS):
        raise R009BuilderError("R009 closure artifact key set differs")
    for key in ARTIFACT_KEYS:
        row = artifacts[key]
        if not isinstance(row, Mapping):
            raise R009BuilderError("R009 closure artifact row is malformed")
        if set(row) != {"path", "sha256"}:
            raise R009BuilderError("R009 closure artifact row fields differ")
        path = _root_relative(root, row.get("path"), f"artifact {key}")
        observed = sha256_file(path) if path.is_file() else None
        if observed != row.get("sha256"):
            raise R009BuilderError("R009 closure artifact digest differs")
        if key in {"script", "txt", "urp"} and row.get("sha256") != identity.controller_triplet_sha256[key]:
            raise R009BuilderError(
                f"R009 closure artifact {key} is not bound to controller triplet"
            )
    content_address = document["content_address"]
    if not isinstance(content_address, Mapping):
        raise R009BuilderError("R009 closure content address is missing")
    basis = dict(document)
    basis.pop("content_address")
    if content_address.get("sha256") != sha256_bytes(canonical_bytes(basis)):
        raise R009BuilderError("R009 closure content address differs")
    return {
        "manifest": manifest,
        "contract": bundle,
        "release_identity": identity,
        "source_closure": source_set,
    }


def load_closure(
    path: Path = DEFAULT_CLOSURE_PATH,
    *,
    root: Path = ROOT,
) -> dict[str, Any]:
    """Purely load and validate a persisted R009 closure."""

    root = _reject_lexical_symlink_ancestors(Path(root), "closure root")
    path = _safe_target_path(root, Path(path), "closure", require_file=True)
    if path.is_symlink() or not path.is_file():
        raise R009BuilderError(f"R009 closure is missing or unsafe: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise R009BuilderError("R009 closure is not strict JSON") from exc
    return validate_closure_document(document, root=root)


def _write_explicit(path: Path, encoded: bytes) -> None:
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise R009BuilderError(f"refusing unsafe R009 persist path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_bytes() == encoded:
        return
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary_path.unlink(missing_ok=True)


def _restore_file_snapshot(
    path: Path,
    snapshot: tuple[bytes, int, int] | None,
) -> None:
    if snapshot is None:
        if path.exists() or path.is_symlink():
            path.unlink()
        return
    encoded, atime_ns, mtime_ns = snapshot
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.rollback.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temporary_path, path)
        except OSError:
            # A fault injected into os.replace must not prevent byte rollback.
            path.write_bytes(encoded)
        os.utime(path, ns=(atime_ns, mtime_ns))
    finally:
        temporary_path.unlink(missing_ok=True)


def _atomic_file_set(
    file_bytes: Mapping[Path, bytes],
    *,
    release_root: Path,
    after_commit: Any | None = None,
) -> None:
    """Commit an explicit file set and restore every prior target on failure."""

    normalized: dict[Path, bytes] = {}
    for raw_path, encoded in file_bytes.items():
        path = _safe_target_path(release_root, Path(raw_path), "persist target")
        if not isinstance(encoded, bytes):
            raise R009BuilderError(f"R009 persist bytes are not bytes: {path}")
        previous = normalized.get(path)
        if previous is not None and previous != encoded:
            raise R009BuilderError(f"R009 persist maps one target to different bytes: {path}")
        normalized[path] = encoded

    snapshots: dict[Path, tuple[bytes, int, int] | None] = {}
    staged: dict[Path, Path] = {}
    try:
        for path, encoded in normalized.items():
            if path.exists():
                if path.is_symlink() or not path.is_file():
                    raise R009BuilderError(f"R009 persist target is unsafe: {path}")
                stat_result = path.stat()
                snapshot = (path.read_bytes(), stat_result.st_atime_ns, stat_result.st_mtime_ns)
            else:
                snapshot = None
            snapshots[path] = snapshot
            if snapshot is not None and snapshot[0] == encoded:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{path.name}.",
                dir=path.parent,
            )
            temporary_path = Path(temporary)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                temporary_path.unlink(missing_ok=True)
                raise
            staged[path] = temporary_path
    except Exception:
        for temporary_path in staged.values():
            temporary_path.unlink(missing_ok=True)
        raise

    replaced: list[Path] = []

    def rollback() -> list[tuple[Path, OSError]]:
        failures: list[tuple[Path, OSError]] = []
        for path in reversed(replaced):
            try:
                _restore_file_snapshot(path, snapshots[path])
            except OSError as exc:
                failures.append((path, exc))
        return failures

    try:
        for path, temporary_path in staged.items():
            _safe_target_path(release_root, path, "persist target")
            os.replace(temporary_path, path)
            replaced.append(path)
            directory_descriptor = os.open(
                path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        if after_commit is not None:
            after_commit()
    except Exception as exc:
        rollback_failures = rollback()
        if rollback_failures:
            raise R009RollbackError(exc, rollback_failures) from exc
        raise
    finally:
        for temporary_path in staged.values():
            temporary_path.unlink(missing_ok=True)


def _read_source_closure_bytes(
    manifest: R009BehaviorManifest,
    *,
    source_root: Path = ROOT,
) -> dict[str, bytes]:
    source_root = _reject_lexical_symlink_ancestors(source_root, "source root")
    source_bytes: dict[str, bytes] = {}
    for relative, expected in manifest.source_set.files.items():
        path = _source_closure_path(source_root, relative)
        if path.is_symlink() or not path.is_file():
            raise R009BuilderError(f"R009 source closure byte digest differs: {relative}")
        encoded = path.read_bytes()
        if sha256_bytes(encoded) != expected:
            raise R009BuilderError(f"R009 source closure byte digest differs: {relative}")
        source_bytes[relative] = encoded
    return source_bytes


def _validate_source_closure(manifest: R009BehaviorManifest) -> None:
    _read_source_closure_bytes(manifest)


def _source_paths_from_result(
    result: Mapping[str, Any],
    *,
    manifest: R009BehaviorManifest,
    temporary_source: tempfile.TemporaryDirectory[str] | None,
) -> tuple[dict[str, Path], tempfile.TemporaryDirectory[str] | None]:
    raw_artifacts = result.get("artifacts")
    if not isinstance(raw_artifacts, Mapping):
        raise R009BuilderError("R009 persist requires build artifact paths")
    if set(raw_artifacts) == set(ARTIFACT_SUFFIXES):
        if not all(isinstance(raw_artifacts[key], (str, Path)) for key in ARTIFACT_SUFFIXES):
            raise R009BuilderError("R009 persist artifact paths are malformed")
        return (
            {key: Path(raw_artifacts[key]) for key in ARTIFACT_SUFFIXES},
            temporary_source,
        )
    if set(raw_artifacts) != set(result.get("controller_triplet_sha256", {})):
        raise R009BuilderError("R009 persist artifact summary is malformed")
    temporary_source = tempfile.TemporaryDirectory(prefix="step5d-r009-persist-source-")
    rebuilt = build(
        Path(temporary_source.name),
        behavior_manifest=manifest,
        stamp=str(result.get("stamp", R009_STAMP)),
    )
    return (
        {key: Path(rebuilt["artifacts"][key]) for key in ARTIFACT_SUFFIXES},
        temporary_source,
    )


def persist_release(
    result: Mapping[str, Any],
    *,
    output_dir: Path,
    contract_path: Path = DEFAULT_CONTRACT_PATH,
    behavior_manifest_path: Path = DEFAULT_BEHAVIOR_MANIFEST_PATH,
    closure_path: Path = DEFAULT_CLOSURE_PATH,
    identity_alias_path: Path = DEFAULT_IDENTITY_ALIAS_PATH,
    release_root: Path = ROOT,
) -> dict[str, object]:
    """Explicitly persist a previously built R009 closure and its triplet."""

    manifest = validate_behavior_manifest(result["behavior_manifest"])
    identity = validate_release_identity(result["release_identity"])
    bundle = validate_contract(result["contract"], release_identity=identity)
    output_dir = _absolute_path(Path(output_dir))
    contract_path = _absolute_path(Path(contract_path))
    behavior_manifest_path = _absolute_path(Path(behavior_manifest_path))
    closure_path = _absolute_path(Path(closure_path))
    identity_alias_path = _absolute_path(Path(identity_alias_path))
    release_root = _reject_lexical_symlink_ancestors(
        Path(release_root), "persist release root"
    )
    for path, role in (
        (output_dir / f"{manifest.program}.script", "triplet"),
        (contract_path, "contract"),
        (behavior_manifest_path, "behavior manifest"),
        (closure_path, "closure"),
        (identity_alias_path, "identity alias"),
    ):
        _safe_target_path(release_root, path, role)

    persisted_identity_path = contract_path.with_name(
        f"{contract_path.stem}.release-identity.json"
    )
    _safe_target_path(release_root, persisted_identity_path, "release identity")
    source_closure_bytes = _read_source_closure_bytes(manifest)
    if bundle.behavior_manifest.as_dict() != manifest.as_dict():
        raise R009BuilderError("R009 persist contract behavior manifest differs")
    if result.get("contract_sha256") is not None and result["contract_sha256"] != bundle.sha256:
        raise R009BuilderError("R009 persist contract digest differs")
    if (
        result.get("release_identity_sha256") is not None
        and result["release_identity_sha256"] != identity.release_identity_sha256
    ):
        raise R009BuilderError("R009 persist release identity digest differs")
    if dict(result.get("controller_triplet_sha256", {})) != dict(identity.controller_triplet_sha256):
        raise R009BuilderError("R009 persist result triplet identity differs")

    temporary_source: tempfile.TemporaryDirectory[str] | None = None
    try:
        source_paths, temporary_source = _source_paths_from_result(
            result,
            manifest=manifest,
            temporary_source=temporary_source,
        )
        source_bytes = _read_source_artifacts(source_paths)
        stamp = str(result.get("stamp", R009_STAMP))
        _validate_source_artifacts(
            source_bytes,
            manifest=manifest,
            identity=identity,
            stamp=stamp,
        )
    except Exception:
        if temporary_source is not None:
            temporary_source.cleanup()
        raise

    target_paths = {
        suffix: output_dir / f"{manifest.program}{suffix}"
        for suffix in ARTIFACT_SUFFIXES
    }
    for path in target_paths.values():
        _safe_target_path(release_root, path, "triplet target")
    contract_encoded = contract_bytes(bundle.raw)
    identity_encoded = identity_bytes(identity)
    behavior_encoded = canonical_bytes(manifest.as_dict()) + b"\n"
    closure = build_closure_document(
        {
            **dict(result),
            "artifacts": {key: str(value) for key, value in target_paths.items()},
            "contract": bundle.contract_document,
            "release_identity": identity.as_dict(),
        },
        artifact_paths=target_paths,
        contract_path=contract_path,
        behavior_manifest_path=behavior_manifest_path,
        identity_path=persisted_identity_path,
        release_root=release_root,
        artifact_bytes=source_bytes,
    )
    closure_bytes = canonical_bytes(closure) + b"\n"
    file_bytes: dict[Path, bytes] = {
        **{
            _safe_target_path(
                release_root,
                release_root / relative,
                f"source target {relative}",
            ): encoded
            for relative, encoded in source_closure_bytes.items()
        },
        **{target_paths[suffix]: source_bytes[suffix] for suffix in ARTIFACT_SUFFIXES},
        contract_path: contract_encoded,
        persisted_identity_path: identity_encoded,
        behavior_manifest_path: behavior_encoded,
        identity_alias_path: identity_encoded,
        closure_path: closure_bytes,
    }

    def cold_read() -> None:
        persisted = load_contract(
            contract_path,
            identity_path=persisted_identity_path,
        )
        if persisted.release_identity.as_dict() != identity.as_dict():
            raise R009BuilderError("R009 persisted identity cold-read differs")
        load_closure(closure_path, root=release_root)

    try:
        _atomic_file_set(file_bytes, release_root=release_root, after_commit=cold_read)
    finally:
        if temporary_source is not None:
            temporary_source.cleanup()
    result_copy = dict(result)
    result_copy.update(
        {
            "output_dir": str(output_dir),
            "artifacts": {key: str(value) for key, value in target_paths.items()},
            "persisted": {
                "contract": str(contract_path),
                "behavior_manifest": str(behavior_manifest_path),
                "release_identity": str(persisted_identity_path),
                "release_identity_alias": str(identity_alias_path),
                "closure": str(closure_path),
                "closure_sha256": hashlib.sha256(closure_bytes).hexdigest(),
            },
        }
    )
    return result_copy


persist = persist_release


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="explicit offline output directory; omitted means pure build_release",
    )
    parser.add_argument(
        "--release-root",
        type=Path,
        default=None,
        help="root containing all persisted release targets",
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help="explicitly persist the R009 contract, manifest, identity, and closure",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.persist and args.output_dir is None:
        parser.error("--persist requires an explicit --output-dir")
    if args.persist:
        result = build_release()
    elif args.output_dir is None:
        result = build_release()
    else:
        result = build(args.output_dir)
    if args.persist:
        output_dir = _absolute_path(args.output_dir)
        release_root = (
            Path(args.release_root).resolve()
            if args.release_root is not None
            else ROOT
            if output_dir == DEFAULT_OUTPUT.resolve()
            else None
        )
        if release_root is None:
            parser.error("out-of-tree --persist requires --release-root")
        result = persist_release(
            result,
            output_dir=output_dir,
            contract_path=release_root / "config/step5d/autotune_v4_r009.json",
            behavior_manifest_path=(
                release_root / "config/step5d/autotune_v4_r009.behavior-manifest.json"
            ),
            closure_path=release_root / "config/step5d/autotune_v4_r009_offline_closure.json",
            identity_alias_path=release_root / "config/step5d/r009_release_identity.json",
            release_root=release_root,
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_BEHAVIOR_MANIFEST_PATH",
    "DEFAULT_CLOSURE_PATH",
    "DEFAULT_CONTRACT_PATH",
    "DEFAULT_IDENTITY_ALIAS_PATH",
    "DEFAULT_OUTPUT",
    "R009BuilderError",
    "R009RollbackError",
    "R009_CLOSURE_SCHEMA",
    "build",
    "build_closure_document",
    "build_release",
    "load_closure",
    "persist",
    "persist_release",
    "validate_closure_document",
]
