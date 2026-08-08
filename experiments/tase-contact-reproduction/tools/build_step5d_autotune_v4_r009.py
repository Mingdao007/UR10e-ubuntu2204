#!/usr/bin/env python3
"""Build an R009 TP triplet into an explicitly supplied offline directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

from step5d_autotune_v4_r009.contracts import (
    DEFAULT_CONTRACT_PATH,
    R009Contract,
    build_contract,
    canonical_bytes,
    contract_bytes,
    identity_bytes,
    load_contract,
    persist_contract,
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
from step5d_autotune_v4_r009.tp import R009_STAMP, build_triplet


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "programs/step5/step5d"
DEFAULT_BEHAVIOR_MANIFEST_PATH = ROOT / "config/step5d/autotune_v4_r009.behavior-manifest.json"
DEFAULT_CLOSURE_PATH = ROOT / "config/step5d/autotune_v4_r009_offline_closure.json"
DEFAULT_IDENTITY_ALIAS_PATH = ROOT / "config/step5d/r009_release_identity.json"
R009_CLOSURE_SCHEMA = "step5d.autotune-v4/r009-offline-closure-v1"
R009_OBSERVATION_BINDING_SCHEMA = "step5d.autotune-v4/r009-observation-release-binding-v1"


class R009BuilderError(RuntimeError):
    """The offline R009 builder cannot prove a complete identity closure."""


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


def _relative_or_string(path: Path) -> str:
    path = Path(path)
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)


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
    output_dir: Path = DEFAULT_OUTPUT,
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
) -> dict[str, Any]:
    """Purely derive the canonical R009 closure document from a build result."""

    identity = validate_release_identity(result["release_identity"])
    manifest = validate_behavior_manifest(result["behavior_manifest"])
    bundle = validate_contract(result["contract"], release_identity=identity)
    paths = artifact_paths
    if paths is None:
        raw_paths = result.get("artifacts")
        if not isinstance(raw_paths, Mapping):
            raise R009BuilderError("R009 closure requires artifact paths")
        paths = {key: Path(value) for key, value in raw_paths.items()}
    artifacts: dict[str, dict[str, object]] = {}
    for suffix in (".script", ".txt", ".urp", ".numeric-sanity.json", ".deploy-manifest.json"):
        path = Path(paths[suffix])
        if path.is_symlink() or not path.is_file():
            raise R009BuilderError(f"R009 closure artifact is missing or unsafe: {path}")
        artifacts[suffix.lstrip(".").replace(".", "_")] = {
            "path": _relative_or_string(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
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
            "path": _relative_or_string(Path(behavior_manifest_path)),
            "sha256": manifest.behavior_manifest_sha256,
        },
        "release_contract": {
            "path": _relative_or_string(Path(contract_path)),
            "sha256": bundle.sha256,
        },
        "release_identity": {
            "path": _relative_or_string(identity_document_path),
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
    candidate = Path(root).resolve() / value
    if candidate.is_symlink():
        raise R009BuilderError(f"R009 closure {role} path is a symlink")
    path = candidate.resolve()
    try:
        path.relative_to(Path(root).resolve())
    except ValueError as exc:
        raise R009BuilderError(f"R009 closure {role} path escapes root") from exc
    return path


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
    root = Path(root).resolve()
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
        path = _root_relative(root, relative, "source")
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
    if not isinstance(artifacts, Mapping):
        raise R009BuilderError("R009 closure artifacts are missing")
    for row in artifacts.values():
        if not isinstance(row, Mapping):
            raise R009BuilderError("R009 closure artifact row is malformed")
        path = _root_relative(root, row.get("path"), "artifact")
        if path.is_symlink() or not path.is_file() or sha256_file(path) != row.get("sha256"):
            raise R009BuilderError("R009 closure artifact digest differs")
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

    path = Path(path)
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
    path.write_bytes(encoded)


def persist_release(
    result: Mapping[str, Any],
    *,
    output_dir: Path = DEFAULT_OUTPUT,
    contract_path: Path = DEFAULT_CONTRACT_PATH,
    behavior_manifest_path: Path = DEFAULT_BEHAVIOR_MANIFEST_PATH,
    closure_path: Path = DEFAULT_CLOSURE_PATH,
    identity_alias_path: Path = DEFAULT_IDENTITY_ALIAS_PATH,
) -> dict[str, object]:
    """Explicitly persist a previously built R009 closure and its triplet."""

    manifest = validate_behavior_manifest(result["behavior_manifest"])
    identity = validate_release_identity(result["release_identity"])
    bundle = validate_contract(result["contract"], release_identity=identity)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_artifacts = result.get("artifacts")
    if not isinstance(raw_artifacts, Mapping):
        raise R009BuilderError("R009 persist requires build artifact paths")
    source_paths = {
        suffix: Path(raw_artifacts[suffix])
        for suffix in (".script", ".txt", ".urp", ".numeric-sanity.json", ".deploy-manifest.json")
        if suffix in raw_artifacts and isinstance(raw_artifacts[suffix], (str, Path))
    }
    if len(source_paths) != 5 or any(not path.is_file() for path in source_paths.values()):
        rebuilt = build(
            output_dir,
            behavior_manifest=manifest,
            stamp=str(result.get("stamp", R009_STAMP)),
        )
        source_paths = {
            suffix: Path(rebuilt["artifacts"][suffix])
            for suffix in (".script", ".txt", ".urp", ".numeric-sanity.json", ".deploy-manifest.json")
        }
    target_paths: dict[str, Path] = {}
    for suffix, source in source_paths.items():
        target = output_dir / f"step5d_strict_rnn_autotune_v4_r009{suffix}"
        if source.resolve() != target.resolve():
            _write_explicit(target, source.read_bytes())
        target_paths[suffix] = target
    actual_triplet = _triplet_hashes(target_paths)
    if actual_triplet != dict(identity.controller_triplet_sha256):
        raise R009BuilderError("persisted R009 triplet differs from release identity")

    contract_path = Path(contract_path)
    persisted_identity_path = contract_path.with_name(
        f"{contract_path.stem}.release-identity.json"
    )
    if (
        contract_path.is_file()
        and not contract_path.is_symlink()
        and persisted_identity_path.is_file()
        and not persisted_identity_path.is_symlink()
        and contract_path.read_bytes() == contract_bytes(bundle.raw)
        and persisted_identity_path.read_bytes() == identity_bytes(bundle.release_identity)
    ):
        persisted = load_contract(
            contract_path,
            identity_path=persisted_identity_path,
        )
    else:
        persisted = persist_contract(
            bundle,
            contract_path,
            identity_path=persisted_identity_path,
        )
    _write_explicit(
        Path(behavior_manifest_path),
        canonical_bytes(manifest.as_dict()) + b"\n",
    )
    identity_bytes_value = identity_bytes(persisted.release_identity)
    _write_explicit(Path(identity_alias_path), identity_bytes_value)
    closure = build_closure_document(
        {
            **dict(result),
            "artifacts": {key: str(value) for key, value in target_paths.items()},
            "contract": persisted.contract_document,
            "release_identity": persisted.release_identity.as_dict(),
        },
        artifact_paths=target_paths,
        contract_path=contract_path,
        behavior_manifest_path=Path(behavior_manifest_path),
        identity_path=persisted.identity_path,
    )
    closure_bytes = canonical_bytes(closure) + b"\n"
    _write_explicit(Path(closure_path), closure_bytes)
    try:
        root_relative_contract = Path(contract_path).resolve().relative_to(ROOT.resolve())
        root_relative_closure = Path(closure_path).resolve().relative_to(ROOT.resolve())
    except ValueError:
        root_relative_contract = None
        root_relative_closure = None
    if root_relative_contract is not None and root_relative_closure is not None:
        load_closure(Path(closure_path), root=ROOT)
    result_copy = dict(result)
    result_copy.update(
        {
            "output_dir": str(output_dir),
            "artifacts": {key: str(value) for key, value in target_paths.items()},
            "persisted": {
                "contract": str(contract_path),
                "behavior_manifest": str(behavior_manifest_path),
                "release_identity": str(persisted.identity_path),
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
        default=DEFAULT_OUTPUT,
        help="explicit offline output directory",
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help="explicitly persist the R009 contract, manifest, identity, and closure",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = build(args.output_dir)
    if args.persist:
        result = persist_release(result, output_dir=args.output_dir)
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
    "R009_CLOSURE_SCHEMA",
    "build",
    "build_closure_document",
    "build_release",
    "load_closure",
    "persist",
    "persist_release",
    "validate_closure_document",
]
