"""Isolated atomic publisher for Step5d manual-hold releases.

This module intentionally does not alter or parameterize the frozen r009
publisher.  Manual bundles live below ``config/step5d/manual`` and switch only
the manual pointer after every staged byte and compatibility view is durable.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
from typing import Any, Callable, Mapping


class ManualAtomicReleaseError(RuntimeError):
    pass


def canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("ascii")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _relative(value: str) -> Path:
    pure = PurePosixPath(value)
    if (
        not value
        or pure.is_absolute()
        or pure.as_posix() != value
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ManualAtomicReleaseError(f"unsafe manual release path: {value!r}")
    return Path(*pure.parts)


def _atomic_replace(path: Path, encoded: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


class ManualAtomicReleasePublisher:
    """Publish one content-addressed manual bundle and its pointer last."""

    def __init__(self, experiment_root: Path) -> None:
        self.root = experiment_root.resolve(strict=True)
        self.release_root = self.root / "config/step5d/manual/releases"
        self.pointer = self.root / "config/step5d/manual/current.json"

    def publish(
        self,
        *,
        manifest: Mapping[str, Any],
        bundle_files: Mapping[str, bytes],
        compatibility_targets: Mapping[str, str],
        stage_verifier: Callable[[Path, Path, str], None],
        crash_hook: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        hook = crash_hook or (lambda _cut: None)
        self.release_root.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".pending-manual-hold-", dir=self.release_root))
        try:
            for relative, encoded in sorted(bundle_files.items()):
                destination = stage / _relative(relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("xb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                _fsync_directory(destination.parent)
            manifest_bytes = canonical_bytes(manifest)
            manifest_path = stage / "manifest.json"
            with manifest_path.open("xb") as handle:
                handle.write(manifest_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            for directory in sorted(
                {path.parent for path in stage.rglob("*") if path.is_file()},
                key=lambda value: len(value.parts),
                reverse=True,
            ):
                _fsync_directory(directory)
            _fsync_directory(stage)
            hook("staging_fsynced")

            digest = hashlib.sha256(manifest_bytes).hexdigest()
            stage_verifier(stage, manifest_path, digest)
            hook("staging_verified")
            bundle = self.release_root / digest
            if bundle.exists():
                if bundle.is_symlink() or not bundle.is_dir() or _tree_bytes(bundle) != _tree_bytes(stage):
                    raise ManualAtomicReleaseError(
                        "content-addressed manual release already contains different bytes"
                    )
                shutil.rmtree(stage)
            else:
                os.rename(stage, bundle)
                _fsync_directory(self.release_root)
            hook("bundle_renamed")

            for index, (target_relative, bundle_relative) in enumerate(
                sorted(compatibility_targets.items()), start=1
            ):
                source = bundle / _relative(bundle_relative)
                if source.is_symlink() or not source.is_file():
                    raise ManualAtomicReleaseError(
                        f"manual compatibility source is missing: {bundle_relative}"
                    )
                _atomic_replace(self.root / _relative(target_relative), source.read_bytes())
                hook(f"compatibility_mirror_{index}")

            pointer = {
                "schema": "step5d.manual-hold/current-release-pointer-v1",
                "manifest_path": (
                    Path("config/step5d/manual/releases") / digest / "manifest.json"
                ).as_posix(),
                "manifest_sha256": digest,
            }
            hook("before_pointer_write")
            _atomic_replace(self.pointer, canonical_bytes(pointer))
            hook("pointer_written")
            _fsync_directory(self.pointer.parent)
            hook("pointer_directory_fsynced")
            return {
                "ok": True,
                "manifest_path": pointer["manifest_path"],
                "manifest_sha256": digest,
                "bundle": str(bundle),
                "pointer": str(self.pointer),
            }
        finally:
            if stage.exists():
                shutil.rmtree(stage)


__all__ = [
    "ManualAtomicReleaseError",
    "ManualAtomicReleasePublisher",
    "canonical_bytes",
]
