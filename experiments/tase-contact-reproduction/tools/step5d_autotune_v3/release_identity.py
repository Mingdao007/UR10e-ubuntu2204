"""Immutable release identity for the active Step5d V3 runtime.

The active selector is deliberately tiny: ``config/step5d/current.json`` may
name one repository-relative manifest and its exact byte SHA-256 only.  Every
runtime consumer loads the same manifest through this module; directory scans,
``latest`` aliases, environment overrides, and compatibility fallbacks are not
part of the active route.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


CURRENT_POINTER_SCHEMA = "step5d.autotune-v3/current-release-pointer-v1"
RELEASE_MANIFEST_SCHEMA = "step5d.autotune-v3/release-manifest-v1"
ROLLING_PROTOCOL = "v3_full_home_rolling_arm_v1"
ROLLING_NORMAL_MAX_RATE_RAD_S = 0.1
ROLLING_EXECUTION_PROFILE_ID = "nf100-slew050-a050"
ROLLING_EXECUTION_PROFILE_INTEGER_ID = 633
RELEASE_STAGE_ID = "step5d_strict_rnn_autotune_v3"
CONTROL_PROFILE_ID = "step5d_strict_rnn_autotune_v1"


class ReleaseIdentityError(RuntimeError):
    """The active release pointer or immutable manifest is not trustworthy."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _strict_object(encoded: bytes, role: str) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ReleaseIdentityError(f"{role} repeats JSON key {key!r}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ReleaseIdentityError(f"{role} contains non-finite {value}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseIdentityError(f"{role} is not strict JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReleaseIdentityError(f"{role} must be a JSON object")
    return payload


def _sha256_text(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ReleaseIdentityError(f"{role} must be a lowercase SHA-256")
    return value


def _relative_path(value: Any, role: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise ReleaseIdentityError(f"{role} must be a non-empty relative path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ReleaseIdentityError(f"{role} must be a normalized relative POSIX path")
    return path


@dataclass(frozen=True)
class ReleaseIdentity:
    program_id: str
    release_stage_id: str
    control_profile_id: str
    protocol_id: str
    normal_max_rate_rad_s: float
    execution_profile_id: str
    execution_profile_integer_id: int
    manifest_path: str
    manifest_sha256: str
    artifacts: Mapping[str, Mapping[str, str]]
    controller_readback: Mapping[str, Any]
    source_fingerprints: Mapping[str, str]
    generated_files: Mapping[str, str]
    compatibility_mirrors: Mapping[str, str]
    verification: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.program_id, str) or not self.program_id:
            raise ReleaseIdentityError("release program_id is missing")
        if self.release_stage_id != RELEASE_STAGE_ID:
            raise ReleaseIdentityError("release stage identity differs")
        if self.control_profile_id != CONTROL_PROFILE_ID:
            raise ReleaseIdentityError("release control profile differs")
        if self.protocol_id != ROLLING_PROTOCOL:
            raise ReleaseIdentityError("active release protocol is not rolling-v1")
        if (
            isinstance(self.normal_max_rate_rad_s, bool)
            or not isinstance(self.normal_max_rate_rad_s, (int, float))
            or not math.isclose(
                float(self.normal_max_rate_rad_s),
                ROLLING_NORMAL_MAX_RATE_RAD_S,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ):
            raise ReleaseIdentityError("rolling-v1 normal max-rate is not frozen at 0.1 rad/s")
        if self.execution_profile_id != ROLLING_EXECUTION_PROFILE_ID:
            raise ReleaseIdentityError("rolling-v1 execution profile differs")
        if self.execution_profile_integer_id != ROLLING_EXECUTION_PROFILE_INTEGER_ID:
            raise ReleaseIdentityError("rolling-v1 execution profile integer ID differs from 633")
        _relative_path(self.manifest_path, "release manifest path")
        _sha256_text(self.manifest_sha256, "release manifest SHA-256")
        if not isinstance(self.artifacts, Mapping) or set(self.artifacts) != {
            ".script",
            ".txt",
            ".urp",
        }:
            raise ReleaseIdentityError("release artifact triplet is incomplete")
        for extension, reference in self.artifacts.items():
            if not isinstance(reference, Mapping) or set(reference) != {"path", "sha256"}:
                raise ReleaseIdentityError(f"release artifact reference differs: {extension}")
            _relative_path(reference["path"], f"release artifact {extension}")
            _sha256_text(reference["sha256"], f"release artifact {extension} SHA-256")
        for role, values in (
            ("source fingerprint", self.source_fingerprints),
            ("generated file", self.generated_files),
            ("compatibility mirror", self.compatibility_mirrors),
        ):
            if not isinstance(values, Mapping) or not values:
                raise ReleaseIdentityError(f"{role} map is empty")
            for name, digest in values.items():
                _relative_path(name, role)
                _sha256_text(digest, f"{role} SHA-256")
        if not isinstance(self.controller_readback, Mapping) or set(
            self.controller_readback
        ) != {"path", "sha256", "triplet_sha256", "fresh_get"}:
            raise ReleaseIdentityError("controller readback reference differs")
        _relative_path(self.controller_readback["path"], "controller readback path")
        _sha256_text(self.controller_readback["sha256"], "controller readback SHA-256")
        if self.controller_readback["fresh_get"] is not True:
            raise ReleaseIdentityError("controller readback is not a fresh GET")
        if self.controller_readback["triplet_sha256"] != self.artifact_sha256:
            raise ReleaseIdentityError("controller readback triplet differs from artifacts")
        if not isinstance(self.verification, Mapping) or self.verification.get(
            "canonical_verifier"
        ) != "independent_script_urp_v1":
            raise ReleaseIdentityError("canonical verifier binding differs")

    @property
    def artifact_sha256(self) -> dict[str, str]:
        return {
            extension: str(reference["sha256"])
            for extension, reference in self.artifacts.items()
        }


def identity_from_manifest(
    manifest: Mapping[str, Any],
    *,
    manifest_path: str,
    manifest_sha256: str,
) -> ReleaseIdentity:
    required = {
        "schema",
        "identity",
        "artifacts",
        "controller_readback",
        "runtime_policy",
        "optimizer_policy",
        "source_fingerprints",
        "generated_files",
        "compatibility_mirrors",
        "verification",
    }
    if set(manifest) != required or manifest.get("schema") != RELEASE_MANIFEST_SCHEMA:
        raise ReleaseIdentityError("release manifest fields or schema differ")
    identity = manifest.get("identity")
    if not isinstance(identity, Mapping) or set(identity) != {
        "program_id",
        "release_stage_id",
        "control_profile_id",
        "protocol_id",
        "normal_max_rate_rad_s",
        "execution_profile_id",
        "execution_profile_integer_id",
    }:
        raise ReleaseIdentityError("release identity fields differ")
    return ReleaseIdentity(
        **identity,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        artifacts=manifest["artifacts"],
        controller_readback=manifest["controller_readback"],
        source_fingerprints=manifest["source_fingerprints"],
        generated_files=manifest["generated_files"],
        compatibility_mirrors=manifest["compatibility_mirrors"],
        verification=manifest["verification"],
    )


def load_current_release(experiment_root: Path) -> ReleaseIdentity:
    root = experiment_root.expanduser().resolve(strict=True)
    pointer_path = root / "config/step5d/current.json"
    if pointer_path.is_symlink() or not pointer_path.is_file():
        raise ReleaseIdentityError("current release pointer is missing or unsafe")
    pointer = _strict_object(pointer_path.read_bytes(), "current release pointer")
    if set(pointer) != {"schema", "manifest_path", "manifest_sha256"}:
        raise ReleaseIdentityError("current release pointer must contain manifest path and SHA only")
    if pointer.get("schema") != CURRENT_POINTER_SCHEMA:
        raise ReleaseIdentityError("current release pointer schema differs")
    relative = _relative_path(pointer["manifest_path"], "current manifest path")
    expected_sha = _sha256_text(pointer["manifest_sha256"], "current manifest SHA-256")
    unresolved = root / relative
    resolved = unresolved.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ReleaseIdentityError("current manifest escapes the experiment root") from exc
    if unresolved.is_symlink() or not resolved.is_file():
        raise ReleaseIdentityError("current manifest is missing or unsafe")
    encoded = resolved.read_bytes()
    actual_sha = _sha256_bytes(encoded)
    if actual_sha != expected_sha:
        raise ReleaseIdentityError("current manifest SHA-256 differs")
    if resolved.parent.name != expected_sha or resolved.name != "manifest.json":
        raise ReleaseIdentityError("current manifest path is not content-addressed")
    manifest = _strict_object(encoded, "current release manifest")
    release = identity_from_manifest(
        manifest,
        manifest_path=relative.as_posix(),
        manifest_sha256=actual_sha,
    )
    for role, references in (
        ("artifact", release.artifacts),
        ("generated", {path: {"path": path, "sha256": digest} for path, digest in release.generated_files.items()}),
        ("source", {path: {"path": path, "sha256": digest} for path, digest in release.source_fingerprints.items()}),
        ("mirror", {path: {"path": path, "sha256": digest} for path, digest in release.compatibility_mirrors.items()}),
        ("readback", {"readback": release.controller_readback}),
    ):
        for reference in references.values():
            path = root / _relative_path(reference["path"], f"{role} path")
            if path.is_symlink() or not path.is_file():
                raise ReleaseIdentityError(f"{role} file is missing or unsafe: {reference['path']}")
            if _sha256_bytes(path.read_bytes()) != reference["sha256"]:
                raise ReleaseIdentityError(f"{role} file fingerprint drifted: {reference['path']}")
    return release


__all__ = [
    "CONTROL_PROFILE_ID",
    "CURRENT_POINTER_SCHEMA",
    "RELEASE_MANIFEST_SCHEMA",
    "RELEASE_STAGE_ID",
    "ROLLING_EXECUTION_PROFILE_ID",
    "ROLLING_EXECUTION_PROFILE_INTEGER_ID",
    "ROLLING_NORMAL_MAX_RATE_RAD_S",
    "ROLLING_PROTOCOL",
    "ReleaseIdentity",
    "ReleaseIdentityError",
    "identity_from_manifest",
    "load_current_release",
]
