"""Immutable certificates for pure release-contract checks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .state import atomic_json, read_strict_json


SCOPE_SCHEMA = "step5d.autotune-v3/release-contract-scope-v1"
CERTIFICATE_SCHEMA = "step5d.autotune-v3/release-contract-certificate-v1"
REFERENCE_SCHEMA = "step5d.autotune-v3/release-contract-certificate-ref-v1"
STORE = Path("release-certificates")
_SHA256 = frozenset("0123456789abcdef")


class ReleaseCertificateError(RuntimeError):
    pass


def canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                dict(value),
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReleaseCertificateError(
            f"release certificate is not canonical JSON: {exc}"
        ) from exc


def sha256_bytes(encoded: bytes) -> str:
    return hashlib.sha256(encoded).hexdigest()


def sha256_file(path: Path, role: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise ReleaseCertificateError(f"{role} is missing or unsafe")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_sha256(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or not set(value) <= _SHA256
    ):
        raise ReleaseCertificateError(f"{role} must be a lowercase SHA-256")
    return value


def release_certificate_scope(
    *,
    subject_kind: str,
    release_manifest_sha256: str,
    source_fingerprint: str,
    source_files_fingerprint: str,
    launcher_sha256: str,
    control_environment_sha256: str,
    runtime_epoch: str,
    contract_profile: str,
) -> dict[str, Any]:
    if subject_kind not in {"autotune_v3", "manual_v2"}:
        raise ReleaseCertificateError("release contract subject differs")
    if contract_profile != "state_machine_contract_v1":
        raise ReleaseCertificateError("release contract profile differs")
    return {
        "schema": SCOPE_SCHEMA,
        "subject_kind": subject_kind,
        "release_manifest_sha256": require_sha256(
            release_manifest_sha256, "release manifest SHA-256"
        ),
        "source_fingerprint": require_sha256(
            source_fingerprint, "release source fingerprint"
        ),
        "source_files_fingerprint": require_sha256(
            source_files_fingerprint, "release source files fingerprint"
        ),
        "launcher_sha256": require_sha256(
            launcher_sha256, "canonical launcher SHA-256"
        ),
        "control_environment_sha256": require_sha256(
            control_environment_sha256, "control environment SHA-256"
        ),
        "runtime_epoch": require_sha256(runtime_epoch, "runtime epoch"),
        "contract_profile": contract_profile,
    }


def scope_sha256(scope: Mapping[str, Any]) -> str:
    validated = validate_scope(scope)
    return sha256_bytes(canonical_bytes(validated))


def validate_scope(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "subject_kind",
        "release_manifest_sha256",
        "source_fingerprint",
        "source_files_fingerprint",
        "launcher_sha256",
        "control_environment_sha256",
        "runtime_epoch",
        "contract_profile",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ReleaseCertificateError("release certificate scope fields differ")
    if value.get("schema") != SCOPE_SCHEMA:
        raise ReleaseCertificateError("release certificate scope schema differs")
    return release_certificate_scope(
        subject_kind=value["subject_kind"],
        release_manifest_sha256=value["release_manifest_sha256"],
        source_fingerprint=value["source_fingerprint"],
        source_files_fingerprint=value["source_files_fingerprint"],
        launcher_sha256=value["launcher_sha256"],
        control_environment_sha256=value["control_environment_sha256"],
        runtime_epoch=value["runtime_epoch"],
        contract_profile=value["contract_profile"],
    )


def _relative(root: Path, path: Path, role: str) -> str:
    resolved_root = root.resolve(strict=True)
    unresolved = path.expanduser()
    if unresolved.is_symlink():
        raise ReleaseCertificateError(f"{role} is unsafe")
    resolved = unresolved.resolve(strict=True)
    try:
        relative = resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ReleaseCertificateError(f"{role} escapes certificate root") from exc
    return PurePosixPath(relative).as_posix()


def certificate_path(root: Path, scope: Mapping[str, Any]) -> Path:
    validated = validate_scope(scope)
    digest = scope_sha256(validated)
    return (
        root.resolve()
        / STORE
        / validated["release_manifest_sha256"]
        / digest
        / "certificate.json"
    )


def write_release_certificate(
    root: Path,
    *,
    scope: Mapping[str, Any],
    contract_evidence_path: Path,
    contract_evidence_sha256: str,
    completed_at_unix_ns: int,
) -> tuple[dict[str, Any], dict[str, str]]:
    store_root = root.resolve(strict=True)
    validated_scope = validate_scope(scope)
    digest = scope_sha256(validated_scope)
    if (
        isinstance(completed_at_unix_ns, bool)
        or not isinstance(completed_at_unix_ns, int)
        or completed_at_unix_ns <= 0
    ):
        raise ReleaseCertificateError(
            "release certificate completion timestamp is invalid"
        )
    evidence_sha256 = require_sha256(
        contract_evidence_sha256, "contract evidence SHA-256"
    )
    if (
        sha256_file(contract_evidence_path, "contract evidence")
        != evidence_sha256
    ):
        raise ReleaseCertificateError("contract evidence SHA-256 differs")
    payload = {
        "schema": CERTIFICATE_SCHEMA,
        "scope": validated_scope,
        "scope_sha256": digest,
        "outcome": "RELEASE_CONTRACT_PROVEN",
        "completed_at_unix_ns": completed_at_unix_ns,
        "contract_evidence": {
            "path": _relative(
                store_root,
                contract_evidence_path,
                "contract evidence",
            ),
            "sha256": evidence_sha256,
        },
    }
    path = certificate_path(store_root, validated_scope)
    if path.exists():
        existing = read_strict_json(path, role="release certificate")
        if existing != payload:
            raise ReleaseCertificateError(
                "immutable release certificate bytes differ"
            )
    else:
        atomic_json(path, payload)
    return payload, {
        "schema": REFERENCE_SCHEMA,
        "path": str(path),
        "sha256": sha256_file(path, "release certificate"),
    }


def load_release_certificate(
    root: Path,
    path: Path,
    *,
    expected_scope: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    store_root = root.resolve(strict=True)
    unresolved = path.expanduser()
    if unresolved.is_symlink():
        raise ReleaseCertificateError("release certificate is unsafe")
    resolved = unresolved.resolve(strict=True)
    try:
        resolved.relative_to(store_root / STORE)
    except ValueError as exc:
        raise ReleaseCertificateError(
            "release certificate escapes the immutable store"
        ) from exc
    payload = read_strict_json(resolved, role="release certificate")
    required = {
        "schema",
        "scope",
        "scope_sha256",
        "outcome",
        "completed_at_unix_ns",
        "contract_evidence",
    }
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise ReleaseCertificateError("release certificate fields differ")
    if (
        payload["schema"] != CERTIFICATE_SCHEMA
        or payload["outcome"] != "RELEASE_CONTRACT_PROVEN"
    ):
        raise ReleaseCertificateError("release certificate schema or outcome differs")
    scope = validate_scope(payload["scope"])
    digest = scope_sha256(scope)
    if payload["scope_sha256"] != digest:
        raise ReleaseCertificateError("release certificate scope digest differs")
    if expected_scope is not None and scope != validate_scope(expected_scope):
        raise ReleaseCertificateError("release certificate scope differs")
    expected_path = certificate_path(store_root, scope).resolve()
    if resolved != expected_path:
        raise ReleaseCertificateError(
            "release certificate path is not scope-addressed"
        )
    completed = payload["completed_at_unix_ns"]
    if (
        isinstance(completed, bool)
        or not isinstance(completed, int)
        or completed <= 0
    ):
        raise ReleaseCertificateError(
            "release certificate completion timestamp is invalid"
        )
    evidence = payload["contract_evidence"]
    if not isinstance(evidence, Mapping) or set(evidence) != {"path", "sha256"}:
        raise ReleaseCertificateError(
            "release certificate contract reference differs"
        )
    relative_text = evidence["path"]
    if not isinstance(relative_text, str):
        raise ReleaseCertificateError(
            "release certificate contract path is unsafe"
        )
    relative = PurePosixPath(relative_text)
    if (
        relative.is_absolute()
        or relative.as_posix() != relative_text
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ReleaseCertificateError(
            "release certificate contract path is unsafe"
        )
    evidence_path = (store_root / Path(*relative.parts)).resolve(strict=True)
    try:
        evidence_path.relative_to(store_root)
    except ValueError as exc:
        raise ReleaseCertificateError(
            "release certificate contract path escapes"
        ) from exc
    evidence_sha256 = require_sha256(
        evidence["sha256"], "contract evidence SHA-256"
    )
    if (
        sha256_file(evidence_path, "contract evidence")
        != evidence_sha256
    ):
        raise ReleaseCertificateError("contract evidence SHA-256 differs")
    return dict(payload), evidence_path, read_strict_json(
        evidence_path,
        role="contract evidence",
    )


__all__ = [
    "CERTIFICATE_SCHEMA",
    "REFERENCE_SCHEMA",
    "ReleaseCertificateError",
    "certificate_path",
    "load_release_certificate",
    "release_certificate_scope",
    "scope_sha256",
    "validate_scope",
    "write_release_certificate",
]
