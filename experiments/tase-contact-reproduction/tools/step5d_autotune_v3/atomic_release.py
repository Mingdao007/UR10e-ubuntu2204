"""Crash-safe content-addressed release publication primitives."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
from typing import Any, Callable, Mapping

from .release_identity import CURRENT_POINTER_SCHEMA


class AtomicReleaseError(RuntimeError):
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
    if not isinstance(value, str):
        raise AtomicReleaseError(f"unsafe release path: {value!r}")
    pure = PurePosixPath(value)
    if (
        not value
        or pure.is_absolute()
        or pure.as_posix() != value
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise AtomicReleaseError(f"unsafe release path: {value!r}")
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


def _sha256_text(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise AtomicReleaseError(f"{role} must be a lowercase SHA-256")
    return value


def _json_object(encoded: bytes, role: str) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise AtomicReleaseError(f"{role} repeats JSON key {key!r}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda value: (_ for _ in ()).throw(
                AtomicReleaseError(f"{role} contains non-finite {value}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AtomicReleaseError(f"{role} is not strict JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise AtomicReleaseError(f"{role} must be a JSON object")
    return payload


@dataclass(frozen=True)
class PendingRelease:
    program_id: str
    protocol_id: str
    normal_max_rate_rad_s: float
    execution_profile_id: str
    execution_profile_integer_id: int


class AtomicReleasePublisher:
    """Publish an immutable bundle and commit it through one digest pointer."""

    def __init__(self, experiment_root: Path) -> None:
        self.root = experiment_root.resolve(strict=True)
        self.release_root = self.root / "config/step5d/releases"
        self.pointer = self.root / "config/step5d/current.json"

    def _pointer_payload(self, digest: str) -> dict[str, str]:
        digest = _sha256_text(digest, "release manifest SHA-256")
        return {
            "schema": CURRENT_POINTER_SCHEMA,
            "manifest_path": (
                Path("config/step5d/releases") / digest / "manifest.json"
            ).as_posix(),
            "manifest_sha256": digest,
        }

    def _current_digest(self) -> str:
        if self.pointer.is_symlink() or not self.pointer.is_file():
            raise AtomicReleaseError("current release pointer is missing or unsafe")
        pointer = _json_object(self.pointer.read_bytes(), "current release pointer")
        if set(pointer) != {"schema", "manifest_path", "manifest_sha256"}:
            raise AtomicReleaseError(
                "current release pointer must contain manifest path and SHA only"
            )
        digest = _sha256_text(
            pointer.get("manifest_sha256"), "current manifest SHA-256"
        )
        if pointer != self._pointer_payload(digest):
            raise AtomicReleaseError("current release pointer is not canonical")
        return digest

    def _immutable_manifest(
        self,
        digest: str,
        *,
        verifier: Callable[[Path, Path, str], None],
    ) -> tuple[Path, dict[str, Any]]:
        digest = _sha256_text(digest, "release manifest SHA-256")
        bundle = self.release_root / digest
        manifest_path = bundle / "manifest.json"
        if bundle.is_symlink() or not bundle.is_dir():
            raise AtomicReleaseError("content-addressed release bundle is missing or unsafe")
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise AtomicReleaseError("immutable release manifest is missing or unsafe")
        encoded = manifest_path.read_bytes()
        if hashlib.sha256(encoded).hexdigest() != digest:
            raise AtomicReleaseError("immutable release manifest SHA-256 differs")
        verifier(bundle, manifest_path, digest)
        return bundle, _json_object(encoded, "immutable release manifest")

    def _prepare_compatibility(
        self,
        bundle: Path,
        targets: Mapping[str, str],
    ) -> list[tuple[str, Path, bytes]]:
        prepared: list[tuple[str, Path, bytes]] = []
        for target_relative, bundle_relative in sorted(targets.items()):
            target_path = self.root / _relative(target_relative)
            if (
                target_path == self.pointer
                or target_path == self.release_root
                or self.release_root in target_path.parents
            ):
                raise AtomicReleaseError(
                    f"compatibility target overlaps release authority: {target_relative}"
                )
            source = bundle / _relative(bundle_relative)
            if source.is_symlink() or not source.is_file():
                raise AtomicReleaseError(
                    f"compatibility source is missing: {bundle_relative}"
                )
            prepared.append((target_relative, target_path, source.read_bytes()))
        return prepared

    @staticmethod
    def _refresh_compatibility(
        prepared: list[tuple[str, Path, bytes]],
        *,
        hook: Callable[[str], None],
        cut_prefix: str = "",
    ) -> list[dict[str, str]]:
        errors: list[dict[str, str]] = []
        for index, (relative, target, encoded) in enumerate(prepared, start=1):
            try:
                _atomic_replace(target, encoded)
            except OSError as exc:
                errors.append(
                    {
                        "path": relative,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            hook(f"{cut_prefix}compatibility_mirror_{index}")
        return errors

    @staticmethod
    def _rollback_compatibility_key(
        manifest: Mapping[str, Any], role: str
    ) -> tuple[str, str, int, dict[str, int]]:
        identity = manifest.get("identity")
        runtime_identity = manifest.get("tp_runtime_identity")
        if not isinstance(identity, Mapping) or not isinstance(
            runtime_identity, Mapping
        ):
            raise AtomicReleaseError(
                f"{role} manifest lacks rollback compatibility fields"
            )
        protocol_id = identity.get("protocol_id")
        release_stage_id = identity.get("release_stage_id")
        protocol_version = runtime_identity.get("protocol_version")
        registers = runtime_identity.get("registers")
        if (
            not isinstance(protocol_id, str)
            or not protocol_id
            or not isinstance(release_stage_id, str)
            or not release_stage_id
            or isinstance(protocol_version, bool)
            or not isinstance(protocol_version, int)
            or not isinstance(registers, Mapping)
            or not registers
            or any(not isinstance(name, str) or not name for name in registers)
            or any(
                isinstance(index, bool) or not isinstance(index, int)
                for index in registers.values()
            )
        ):
            raise AtomicReleaseError(
                f"{role} manifest lacks rollback compatibility fields"
            )
        return (
            protocol_id,
            release_stage_id,
            protocol_version,
            dict(registers),
        )

    def _stage_immutable_bundle(
        self,
        *,
        manifest: Mapping[str, Any],
        bundle_files: Mapping[str, bytes],
        stage_verifier: Callable[[Path, Path, str], None] | None,
        hook: Callable[[str], None],
    ) -> tuple[str, Path]:
        self.release_root.mkdir(parents=True, exist_ok=True)
        stage = Path(
            tempfile.mkdtemp(prefix=".pending-r009-", dir=self.release_root)
        )
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
            if stage_verifier is None:
                raise AtomicReleaseError(
                    "independent staged-byte verifier is required before publication"
                )
            stage_verifier(stage, manifest_path, digest)
            hook("staging_verified")
            bundle = self.release_root / digest
            if bundle.exists():
                if (
                    bundle.is_symlink()
                    or not bundle.is_dir()
                    or _tree_bytes(bundle) != _tree_bytes(stage)
                ):
                    raise AtomicReleaseError(
                        "content-addressed release path already contains different bytes"
                    )
                shutil.rmtree(stage)
            else:
                os.rename(stage, bundle)
                _fsync_directory(self.release_root)
            hook("bundle_renamed")
            return digest, bundle
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    def stage_candidate(
        self,
        *,
        manifest: Mapping[str, Any],
        bundle_files: Mapping[str, bytes],
        stage_verifier: Callable[[Path, Path, str], None] | None = None,
        crash_hook: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Verify and retain an immutable candidate without changing current truth."""

        hook = crash_hook or (lambda _cut: None)
        digest, bundle = self._stage_immutable_bundle(
            manifest=manifest,
            bundle_files=bundle_files,
            stage_verifier=stage_verifier,
            hook=hook,
        )
        return {
            "ok": True,
            "manifest_path": self._pointer_payload(digest)["manifest_path"],
            "manifest_sha256": digest,
            "bundle": str(bundle),
            "pointer": None,
            "current_pointer_changed": False,
            "compatibility_mirrors_changed": False,
        }

    def publish(
        self,
        *,
        manifest: Mapping[str, Any],
        bundle_files: Mapping[str, bytes],
        compatibility_targets: Mapping[str, str],
        stage_verifier: Callable[[Path, Path, str], None] | None = None,
        crash_hook: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        hook = crash_hook or (lambda _cut: None)
        digest, bundle = self._stage_immutable_bundle(
            manifest=manifest,
            bundle_files=bundle_files,
            stage_verifier=stage_verifier,
            hook=hook,
        )

        prepared = self._prepare_compatibility(bundle, compatibility_targets)
        hook("compatibility_prepared")
        pointer = self._pointer_payload(digest)
        hook("before_pointer_write")
        _atomic_replace(self.pointer, canonical_bytes(pointer))
        hook("pointer_written")
        _fsync_directory(self.pointer.parent)
        hook("pointer_directory_fsynced")
        mirror_errors = self._refresh_compatibility(prepared, hook=hook)
        return {
            "ok": True,
            "manifest_path": pointer["manifest_path"],
            "manifest_sha256": digest,
            "bundle": str(bundle),
            "pointer": str(self.pointer),
            "compatibility_mirror_errors": mirror_errors,
        }

    def rollback(
        self,
        *,
        target_manifest_sha256: str,
        release_verifier: Callable[[Path, Path, str], None] | None = None,
        crash_hook: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        if release_verifier is None:
            raise AtomicReleaseError(
                "independent immutable-release verifier is required before rollback"
            )
        hook = crash_hook or (lambda _cut: None)
        current_digest = self._current_digest()
        target_digest = _sha256_text(
            target_manifest_sha256, "rollback target manifest SHA-256"
        )
        if target_digest == current_digest:
            raise AtomicReleaseError("rollback target is already current")

        _, current_manifest = self._immutable_manifest(
            current_digest, verifier=release_verifier
        )
        hook("rollback_current_verified")
        target_bundle, target_manifest = self._immutable_manifest(
            target_digest, verifier=release_verifier
        )
        hook("rollback_target_verified")
        if self._rollback_compatibility_key(
            current_manifest, "current"
        ) != self._rollback_compatibility_key(target_manifest, "target"):
            raise AtomicReleaseError(
                "rollback target is not protocol-compatible with current release"
            )
        hook("rollback_compatibility_verified")

        mirrors = target_manifest.get("compatibility_mirrors")
        compatibility_targets = (
            {str(relative): str(relative) for relative in mirrors}
            if isinstance(mirrors, Mapping)
            else {}
        )
        prepared = self._prepare_compatibility(target_bundle, compatibility_targets)
        hook("rollback_compatibility_prepared")
        pointer = self._pointer_payload(target_digest)
        hook("rollback_before_pointer_write")
        _atomic_replace(self.pointer, canonical_bytes(pointer))
        hook("rollback_pointer_written")
        _fsync_directory(self.pointer.parent)
        hook("rollback_pointer_directory_fsynced")
        mirror_errors = self._refresh_compatibility(
            prepared,
            hook=hook,
            cut_prefix="rollback_",
        )
        return {
            "ok": True,
            "rolled_back_from_manifest_sha256": current_digest,
            "manifest_path": pointer["manifest_path"],
            "manifest_sha256": target_digest,
            "bundle": str(target_bundle),
            "pointer": str(self.pointer),
            "compatibility_mirror_errors": mirror_errors,
        }


__all__ = [
    "AtomicReleaseError",
    "AtomicReleasePublisher",
    "PendingRelease",
    "canonical_bytes",
]
