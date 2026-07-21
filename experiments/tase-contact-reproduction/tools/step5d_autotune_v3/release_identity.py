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
import ipaddress
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping

from .runtime_identity import (
    RuntimeIdentityError,
    identity_from_manifest as tp_identity_from_manifest,
)
from .source_closure import production_source_closure


CURRENT_POINTER_SCHEMA = "step5d.autotune-v3/current-release-pointer-v1"
RELEASE_MANIFEST_SCHEMA = "step5d.autotune-v3/release-manifest-v3"
ROLLING_PROTOCOL = "v3_full_home_rolling_arm_v1"
ROLLING_NORMAL_MAX_RATE_RAD_S = 0.1
ROLLING_EXECUTION_PROFILE_ID = "nf100-slew050-a050"
ROLLING_EXECUTION_PROFILE_INTEGER_ID = 633
(
    REQUIRED_EXPERIMENT_SOURCE_FINGERPRINTS,
    REQUIRED_REPOSITORY_SOURCE_FINGERPRINTS,
) = production_source_closure(Path(__file__).resolve().parents[2])
RELEASE_STAGE_ID = "step5d_strict_rnn_autotune_v3"
CONTROL_PROFILE_ID = "step5d_strict_rnn_autotune_v1"
ACTIVE_TP_PROGRAM_ID = "step5d_strict_rnn_autotune_v3_r010"
SAFETY_ENVELOPE_PATH = "config/step5/step5d_autotune_v3_control_contract.json"
LAUNCH_PROFILE_PATH = "config/step5/step5d_autotune_v3_launch_profile.json"
QUALIFICATION_RELEASE_MANIFEST_ENV = "STEP5D_V3_QUALIFICATION_RELEASE_MANIFEST"
QUALIFICATION_ENDPOINT_CONFIG_ENV = "STEP5D_V3_QUALIFICATION_ENDPOINT_CONFIG"
QUALIFICATION_MODE_ENV = "STEP5D_V3_QUALIFICATION_MODE"
QUALIFICATION_MODE_VALUE = "endpoint-only-no-motion-v1"
LOCAL_RELEASE_CANDIDATE_SCHEMA = "step5d.autotune-v3/local-release-candidate-v1"


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
    controller_target: str
    tp_runtime_identity: Mapping[str, Any]
    safety_envelope: Mapping[str, str]
    source_fingerprints: Mapping[str, str]
    generated_files: Mapping[str, str]
    verification: Mapping[str, Any]

    def __post_init__(self) -> None:
        if re.fullmatch(r"step5d_strict_rnn_autotune_v3_r\d{3}", self.program_id) is None:
            raise ReleaseIdentityError("active release TP program identity differs")
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
        ):
            if not isinstance(values, Mapping) or not values:
                raise ReleaseIdentityError(f"{role} map is empty")
            for name, digest in values.items():
                _relative_path(name, role)
                _sha256_text(digest, f"{role} SHA-256")
        if set(self.source_fingerprints) != REQUIRED_EXPERIMENT_SOURCE_FINGERPRINTS:
            raise ReleaseIdentityError("experiment source fingerprint coverage differs")
        if (
            not isinstance(self.controller_target, str)
            or not self.controller_target.startswith("/")
            or PurePosixPath(self.controller_target).name.lower()
            != f"{self.program_id}.urp".lower()
        ):
            raise ReleaseIdentityError("controller target differs from release program")
        if not isinstance(self.tp_runtime_identity, Mapping):
            raise ReleaseIdentityError("TP runtime identity is missing")
        try:
            tp_identity, script_artifact_sha256 = tp_identity_from_manifest(
                self.tp_runtime_identity
            )
        except RuntimeIdentityError as exc:
            raise ReleaseIdentityError(str(exc)) from exc
        if (
            tp_identity.program_id != self.program_id
            or tp_identity.protocol_id != self.protocol_id
        ):
            raise ReleaseIdentityError("TP runtime identity release binding differs")
        if script_artifact_sha256 != self.artifact_sha256[".script"]:
            raise ReleaseIdentityError("TP runtime identity script artifact differs")
        if not isinstance(self.safety_envelope, Mapping) or set(
            self.safety_envelope
        ) != {"path", "sha256"}:
            raise ReleaseIdentityError("safety envelope reference differs")
        if self.safety_envelope["path"] != SAFETY_ENVELOPE_PATH:
            raise ReleaseIdentityError("safety envelope path differs")
        _sha256_text(self.safety_envelope["sha256"], "safety envelope SHA-256")
        if not isinstance(self.verification, Mapping) or set(self.verification) != {
            "canonical_verifier",
            "staged_bytes_required",
            "pointer_switched_last",
            "runtime_identity_derivation",
            "repository_source_root_depth",
            "repository_source_fingerprints",
        }:
            raise ReleaseIdentityError("release verification fields differ")
        if (
            self.verification["canonical_verifier"] != "independent_script_urp_v3"
            or self.verification["staged_bytes_required"] is not True
            or self.verification["pointer_switched_last"] is not True
            or self.verification["runtime_identity_derivation"]
            != "canonical_script_identity_basis_sha256_plus_final_artifact_sha256_v1"
        ):
            raise ReleaseIdentityError("canonical verifier binding differs")
        repository_sources = self.verification.get("repository_source_fingerprints")
        repository_depth = self.verification.get("repository_source_root_depth")
        if (
            not isinstance(repository_sources, Mapping)
            or set(repository_sources) != REQUIRED_REPOSITORY_SOURCE_FINGERPRINTS
            or isinstance(repository_depth, bool)
            or not isinstance(repository_depth, int)
            or not 0 <= repository_depth <= 4
        ):
            raise ReleaseIdentityError("repository source fingerprint coverage differs")
        for path, digest in repository_sources.items():
            _relative_path(path, "repository source fingerprint")
            _sha256_text(digest, "repository source fingerprint SHA-256")

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
        "controller_target",
        "tp_runtime_identity",
        "safety_envelope",
        "source_fingerprints",
        "generated_files",
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
        controller_target=manifest["controller_target"],
        tp_runtime_identity=manifest["tp_runtime_identity"],
        safety_envelope=manifest["safety_envelope"],
        source_fingerprints=manifest["source_fingerprints"],
        generated_files=manifest["generated_files"],
        verification=manifest["verification"],
    )


def _load_release_reference(
    root: Path,
    relative: PurePosixPath,
    expected_sha: str,
    *,
    role: str,
) -> ReleaseIdentity:
    unresolved = root / relative
    resolved = unresolved.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ReleaseIdentityError(f"{role} manifest escapes the experiment root") from exc
    if unresolved.is_symlink() or not resolved.is_file():
        raise ReleaseIdentityError(f"{role} manifest is missing or unsafe")
    encoded = resolved.read_bytes()
    actual_sha = _sha256_bytes(encoded)
    if actual_sha != expected_sha:
        raise ReleaseIdentityError(f"{role} manifest SHA-256 differs")
    if resolved.parent.name != expected_sha or resolved.name != "manifest.json":
        raise ReleaseIdentityError(f"{role} manifest path is not content-addressed")
    manifest = _strict_object(encoded, f"{role} release manifest")
    release = identity_from_manifest(
        manifest,
        manifest_path=relative.as_posix(),
        manifest_sha256=actual_sha,
    )
    for role, references in (
        ("artifact", release.artifacts),
        ("generated", {path: {"path": path, "sha256": digest} for path, digest in release.generated_files.items()}),
        ("safety envelope", {"safety_envelope": release.safety_envelope}),
    ):
        for reference in references.values():
            unresolved_payload = resolved.parent / _relative_path(
                reference["path"], f"{role} path"
            )
            path = unresolved_payload.resolve()
            try:
                path.relative_to(resolved.parent)
            except ValueError as exc:
                raise ReleaseIdentityError(
                    f"{role} file escapes immutable bundle: {reference['path']}"
                ) from exc
            if unresolved_payload.is_symlink() or not path.is_file():
                raise ReleaseIdentityError(f"{role} file is missing or unsafe: {reference['path']}")
            if _sha256_bytes(path.read_bytes()) != reference["sha256"]:
                raise ReleaseIdentityError(f"{role} file fingerprint drifted: {reference['path']}")
    for relative, expected in release.source_fingerprints.items():
        path = root / _relative_path(relative, "source fingerprint path")
        if path.is_symlink() or not path.is_file():
            raise ReleaseIdentityError(
                f"source file is missing or unsafe: {relative}"
            )
        if _sha256_bytes(path.read_bytes()) != expected:
            raise ReleaseIdentityError(
                f"source file fingerprint drifted: {relative}"
            )
    repository_root = root
    for _ in range(release.verification["repository_source_root_depth"]):
        repository_root = repository_root.parent
    repository_root = repository_root.resolve(strict=True)
    for relative, expected in release.verification[
        "repository_source_fingerprints"
    ].items():
        path = repository_root / _relative_path(
            relative, "repository source fingerprint"
        )
        if path.is_symlink() or not path.is_file():
            raise ReleaseIdentityError(
                f"repository source file is missing or unsafe: {relative}"
            )
        if _sha256_bytes(path.read_bytes()) != expected:
            raise ReleaseIdentityError(
                f"repository source file fingerprint drifted: {relative}"
            )
    return release


def load_release_manifest(
    experiment_root: Path,
    manifest_path: Path,
    *,
    expected_manifest_sha256: str | None = None,
) -> ReleaseIdentity:
    """Load one explicit content-addressed manifest without consulting current."""

    root = experiment_root.expanduser().resolve(strict=True)
    unresolved = manifest_path.expanduser()
    if not unresolved.is_absolute():
        unresolved = root / unresolved
    if unresolved.is_symlink():
        raise ReleaseIdentityError("explicit release manifest is unsafe")
    resolved = unresolved.resolve(strict=True)
    try:
        relative = PurePosixPath(resolved.relative_to(root).as_posix())
    except ValueError as exc:
        raise ReleaseIdentityError(
            "explicit release manifest escapes the experiment root"
        ) from exc
    actual_sha = _sha256_bytes(resolved.read_bytes())
    expected_sha = (
        actual_sha
        if expected_manifest_sha256 is None
        else _sha256_text(
            expected_manifest_sha256, "explicit release manifest SHA-256"
        )
    )
    return _load_release_reference(
        root,
        relative,
        expected_sha,
        role="explicit release",
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
    return _load_release_reference(
        root,
        relative,
        expected_sha,
        role="current",
    )


def load_local_release_candidate(
    experiment_root: Path,
    descriptor_path: Path,
) -> tuple[ReleaseIdentity, dict[str, Any]]:
    root = experiment_root.expanduser().resolve(strict=True)
    unresolved = descriptor_path.expanduser()
    if unresolved.is_symlink() or not unresolved.is_file():
        raise ReleaseIdentityError("local release candidate descriptor is missing or unsafe")
    descriptor = _strict_object(
        unresolved.read_bytes(), "local release candidate descriptor"
    )
    if set(descriptor) != {
        "schema",
        "ok",
        "program",
        "manifest_path",
        "manifest_sha256",
        "bundle",
        "pointer",
        "current_pointer_changed",
        "compatibility_mirrors_changed",
        "verification",
    }:
        raise ReleaseIdentityError("local release candidate descriptor fields differ")
    if (
        descriptor.get("schema") != LOCAL_RELEASE_CANDIDATE_SCHEMA
        or descriptor.get("ok") is not True
        or descriptor.get("program") != ACTIVE_TP_PROGRAM_ID
        or descriptor.get("pointer") is not None
        or descriptor.get("current_pointer_changed") is not False
        or descriptor.get("compatibility_mirrors_changed") is not False
    ):
        raise ReleaseIdentityError("local release candidate descriptor state differs")
    manifest_sha256 = _sha256_text(
        descriptor.get("manifest_sha256"), "local candidate manifest SHA-256"
    )
    manifest_path = _relative_path(
        descriptor.get("manifest_path"), "local candidate manifest path"
    )
    release = load_release_manifest(
        root,
        Path(manifest_path.as_posix()),
        expected_manifest_sha256=manifest_sha256,
    )
    if release.program_id != descriptor["program"]:
        raise ReleaseIdentityError("local candidate program binding differs")
    if Path(str(descriptor.get("bundle"))).resolve() != (
        root / manifest_path
    ).resolve().parent:
        raise ReleaseIdentityError("local candidate bundle binding differs")
    if not isinstance(descriptor.get("verification"), Mapping):
        raise ReleaseIdentityError("local candidate verification evidence is missing")
    return release, descriptor


def qualification_runtime_environment(
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    values = os.environ if environment is None else environment
    names = (
        QUALIFICATION_RELEASE_MANIFEST_ENV,
        QUALIFICATION_ENDPOINT_CONFIG_ENV,
        QUALIFICATION_MODE_ENV,
    )
    present = {name: values.get(name, "") for name in names}
    if not any(present.values()):
        return {}
    if not all(present.values()):
        raise ReleaseIdentityError("qualification release environment is incomplete")
    return present


def load_runtime_release(
    experiment_root: Path,
    environment: Mapping[str, str] | None = None,
) -> ReleaseIdentity:
    """Use an explicit release only inside the localhost no-motion qualification seam."""

    root = experiment_root.expanduser().resolve(strict=True)
    values = os.environ if environment is None else environment
    qualification = qualification_runtime_environment(values)
    if not qualification:
        return load_current_release(root)
    if qualification[QUALIFICATION_MODE_ENV] != QUALIFICATION_MODE_VALUE:
        raise ReleaseIdentityError("qualification release mode differs")
    if values.get("STEP5D_V3_CANONICAL_LAUNCHER") != str(
        (root / "scripts/step5d-autotune-v3.sh").resolve()
    ):
        raise ReleaseIdentityError("qualification release lacks canonical launcher binding")
    endpoint_path = Path(qualification[QUALIFICATION_ENDPOINT_CONFIG_ENV]).expanduser()
    if endpoint_path.is_symlink() or not endpoint_path.is_file():
        raise ReleaseIdentityError("qualification endpoint config is missing or unsafe")
    endpoint = _strict_object(
        endpoint_path.read_bytes(), "qualification endpoint config"
    )
    if set(endpoint) != {"schema", "content_sha256", "addresses", "motion_capable"}:
        raise ReleaseIdentityError("qualification endpoint config fields differ")
    if (
        endpoint.get("schema")
        != "step5d.autotune-v3/qualification-endpoint-config-v1"
        or endpoint.get("motion_capable") is not False
    ):
        raise ReleaseIdentityError("qualification endpoint config is not no-motion")
    _sha256_text(endpoint.get("content_sha256"), "qualification endpoint content SHA-256")
    addresses = endpoint.get("addresses")
    if not isinstance(addresses, Mapping) or set(addresses) != {
        "dashboard",
        "secondary",
        "rtde",
        "kunwei",
    }:
        raise ReleaseIdentityError("qualification endpoint address roles differ")
    for role, address in addresses.items():
        if not isinstance(address, Mapping) or set(address) != {"host", "port"}:
            raise ReleaseIdentityError(f"qualification {role} address fields differ")
        try:
            host = ipaddress.ip_address(address.get("host"))
        except ValueError as exc:
            raise ReleaseIdentityError(
                f"qualification {role} host is not a loopback literal"
            ) from exc
        port = address.get("port")
        if (
            host.version != 4
            or not host.is_loopback
            or isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
        ):
            raise ReleaseIdentityError(
                f"qualification {role} endpoint is not loopback-only"
            )
    return load_release_manifest(
        root,
        Path(qualification[QUALIFICATION_RELEASE_MANIFEST_ENV]),
    )


def release_payload_path(
    experiment_root: Path,
    release: ReleaseIdentity,
    relative_path: str,
) -> Path:
    """Return one verified payload from the release's immutable bundle."""

    root = experiment_root.expanduser().resolve(strict=True)
    manifest_path = (
        root / _relative_path(release.manifest_path, "release manifest path")
    ).resolve(strict=True)
    try:
        manifest_path.relative_to(root)
    except ValueError as exc:
        raise ReleaseIdentityError("release manifest escapes the experiment root") from exc
    if (
        manifest_path.name != "manifest.json"
        or manifest_path.parent.name != release.manifest_sha256
        or _sha256_bytes(manifest_path.read_bytes()) != release.manifest_sha256
    ):
        raise ReleaseIdentityError("release manifest identity differs")

    relative = _relative_path(relative_path, "release payload path")
    expected_sha: str | None = release.generated_files.get(relative.as_posix())
    if relative.as_posix() == release.safety_envelope.get("path"):
        safety_sha = str(release.safety_envelope.get("sha256"))
        if expected_sha is not None and expected_sha != safety_sha:
            raise ReleaseIdentityError("release payload has conflicting fingerprints")
        expected_sha = safety_sha
    for reference in release.artifacts.values():
        if relative.as_posix() == reference.get("path"):
            artifact_sha = str(reference.get("sha256"))
            if expected_sha is not None and expected_sha != artifact_sha:
                raise ReleaseIdentityError("release payload has conflicting fingerprints")
            expected_sha = artifact_sha
    if expected_sha is None:
        raise ReleaseIdentityError(
            f"release payload is not manifest-bound: {relative.as_posix()}"
        )
    _sha256_text(expected_sha, "release payload SHA-256")

    unresolved = manifest_path.parent / relative
    resolved = unresolved.resolve()
    try:
        resolved.relative_to(manifest_path.parent)
    except ValueError as exc:
        raise ReleaseIdentityError("release payload escapes immutable bundle") from exc
    if unresolved.is_symlink() or not resolved.is_file():
        raise ReleaseIdentityError("release payload is missing or unsafe")
    if _sha256_bytes(resolved.read_bytes()) != expected_sha:
        raise ReleaseIdentityError("release payload fingerprint drifted")
    return resolved


__all__ = [
    "ACTIVE_TP_PROGRAM_ID",
    "CONTROL_PROFILE_ID",
    "CURRENT_POINTER_SCHEMA",
    "LAUNCH_PROFILE_PATH",
    "LOCAL_RELEASE_CANDIDATE_SCHEMA",
    "QUALIFICATION_ENDPOINT_CONFIG_ENV",
    "QUALIFICATION_MODE_ENV",
    "QUALIFICATION_MODE_VALUE",
    "QUALIFICATION_RELEASE_MANIFEST_ENV",
    "RELEASE_MANIFEST_SCHEMA",
    "RELEASE_STAGE_ID",
    "REQUIRED_EXPERIMENT_SOURCE_FINGERPRINTS",
    "REQUIRED_REPOSITORY_SOURCE_FINGERPRINTS",
    "ROLLING_EXECUTION_PROFILE_ID",
    "ROLLING_EXECUTION_PROFILE_INTEGER_ID",
    "ROLLING_NORMAL_MAX_RATE_RAD_S",
    "ROLLING_PROTOCOL",
    "SAFETY_ENVELOPE_PATH",
    "ReleaseIdentity",
    "ReleaseIdentityError",
    "identity_from_manifest",
    "load_current_release",
    "load_local_release_candidate",
    "load_release_manifest",
    "load_runtime_release",
    "qualification_runtime_environment",
    "release_payload_path",
]
