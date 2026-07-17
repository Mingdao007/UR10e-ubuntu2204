#!/usr/bin/env python3
"""Content-addressed UR10e artifact storage with immutable SHA256 refs."""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ARTIFACT_STORE_ENV = "UR10E_ARTIFACT_STORE"
ARTIFACT_STORE_DIRNAME = "ur10e-artifacts"


class ArtifactStoreError(ValueError):
    """Raised when an artifact ref or object fails closed."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_common_dir(root: Path) -> Path:
    completed = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise ArtifactStoreError(
            completed.stderr.strip() or f"cannot resolve Git common dir from {root}"
        )
    path = Path(completed.stdout.strip()).expanduser()
    if not path.is_absolute():
        raise ArtifactStoreError(f"Git returned a non-absolute common dir: {path}")
    return path.resolve()


def artifact_store(root: Path, override: Path | None = None) -> Path:
    """Resolve the store from an explicit override, env, or Git common dir."""
    if override is not None:
        return override.expanduser().resolve()
    configured = os.environ.get(ARTIFACT_STORE_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    return git_common_dir(root.resolve()) / ARTIFACT_STORE_DIRNAME


@dataclass(frozen=True)
class ArtifactRef:
    """A SHA256-authoritative reference; ``store_key`` cannot redirect it."""

    sha256: str
    size: int
    store_key: str

    @classmethod
    def from_mapping(cls, payload: Any) -> "ArtifactRef":
        if not isinstance(payload, dict):
            raise ArtifactStoreError("artifact ref must be an object")
        digest = payload.get("sha256")
        size = payload.get("size")
        store_key = payload.get("store_key")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ArtifactStoreError("artifact ref sha256 must be 64 lowercase hex characters")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ArtifactStoreError("artifact ref size must be a non-negative integer")
        authoritative_key = f"sha256/{digest}"
        if store_key != authoritative_key:
            raise ArtifactStoreError("artifact store_key must be derived from sha256")
        return cls(sha256=digest, size=size, store_key=authoritative_key)

    @classmethod
    def for_path(cls, path: Path) -> "ArtifactRef":
        digest = sha256(path)
        return cls(
            sha256=digest,
            size=path.stat().st_size,
            store_key=f"sha256/{digest}",
        )

    def as_dict(self) -> dict[str, str | int]:
        return {"sha256": self.sha256, "size": self.size, "store_key": self.store_key}


def resolve_artifact(ref: ArtifactRef, *, store: Path) -> Path:
    path = store / ref.store_key
    if not path.is_file():
        raise ArtifactStoreError(f"artifact store object missing: {path}")
    if path.stat().st_size != ref.size:
        raise ArtifactStoreError(f"artifact size mismatch: {ref.sha256}")
    if sha256(path) != ref.sha256:
        raise ArtifactStoreError(f"artifact sha256 mismatch: {ref.sha256}")
    return path


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_artifact(source: Path, *, store: Path) -> ArtifactRef:
    """Publish one immutable object under a digest-scoped process lock."""
    source_ref = ArtifactRef.for_path(source)
    object_dir = store / "sha256"
    lock_dir = store / ".locks"
    object_dir.mkdir(parents=True, exist_ok=True)
    lock_dir.mkdir(parents=True, exist_ok=True)
    destination = object_dir / source_ref.sha256
    lock_path = lock_dir / f"{source_ref.sha256}.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if destination.exists():
            resolve_artifact(source_ref, store=store)
            return source_ref
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w+b",
                prefix=f".{source_ref.sha256}.",
                suffix=".tmp",
                dir=object_dir,
                delete=False,
            ) as output:
                temporary = Path(output.name)
                with source.open("rb") as stream:
                    shutil.copyfileobj(stream, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            copied_ref = ArtifactRef.for_path(temporary)
            if copied_ref != source_ref:
                raise ArtifactStoreError("artifact source changed while it was being published")
            temporary.chmod(0o444)
            os.replace(temporary, destination)
            temporary = None
            _fsync_directory(object_dir)
            resolve_artifact(source_ref, store=store)
            return source_ref
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
