"""Independent byte-level verifier for an immutable Step5d rolling release."""

from __future__ import annotations

import gzip
import hashlib
import html
import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
import xml.etree.ElementTree as ET

from .release_identity import (
    ROLLING_EXECUTION_PROFILE_BINDINGS,
    REQUIRED_EXPERIMENT_SOURCE_FINGERPRINTS,
    REQUIRED_REPOSITORY_SOURCE_FINGERPRINTS,
    ReleaseIdentity,
    ReleaseIdentityError,
    identity_from_manifest,
)
from .release_identity import _strict_object as _load_strict_object
from .runtime_identity import RuntimeIdentityError, bind_final_script


class ReleaseVerificationError(RuntimeError):
    """Staged or active bytes do not implement the declared release."""


def _sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ReleaseVerificationError(f"required regular file is missing: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve(
    root: Path,
    relative: str,
    overrides: Mapping[str, Path],
) -> Path:
    if relative in overrides:
        path = overrides[relative]
    else:
        path = root / relative
    resolved = path.resolve()
    if relative not in overrides:
        try:
            resolved.relative_to(root.resolve())
        except ValueError as exc:
            raise ReleaseVerificationError(f"release path escapes root: {relative}") from exc
    return resolved


def _registers(pattern: str, script: str) -> set[int]:
    return {int(value) for value in re.findall(pattern, script)}


def _state_write_order(script: str) -> tuple[int, ...]:
    match = re.search(
        r"^def codex_autotune_write_state\([^\n]*\):\n(?P<body>.*?)^end$",
        script,
        flags=re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise ReleaseVerificationError("script lacks the TP state publication function")
    return tuple(
        int(value)
        for value in re.findall(
            r"write_output_integer_register\(\s*(\d+)\s*,",
            match.group("body"),
        )
    )


def _function_body(script: str, name: str) -> str:
    match = re.search(
        rf"^def {re.escape(name)}\([^\n]*\):\n(?P<body>.*?)^end$",
        script,
        flags=re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise ReleaseVerificationError(f"script lacks function {name}")
    return match.group("body")


def _single_generated(
    release: ReleaseIdentity,
    suffix: str,
) -> tuple[str, str]:
    matches = [
        (path, digest)
        for path, digest in release.generated_files.items()
        if path.endswith(suffix)
    ]
    if len(matches) != 1:
        raise ReleaseVerificationError(
            f"release generated-file binding is not unique: {suffix}"
        )
    return matches[0]


def verify_release_manifest(
    experiment_root: Path,
    manifest_path: Path,
    *,
    expected_manifest_sha256: str | None = None,
    path_overrides: Mapping[str, Path] | None = None,
) -> dict[str, Any]:
    root = experiment_root.resolve(strict=True)
    overrides = dict(path_overrides or {})
    encoded = manifest_path.read_bytes()
    manifest_sha256 = hashlib.sha256(encoded).hexdigest()
    if expected_manifest_sha256 is not None and manifest_sha256 != expected_manifest_sha256:
        raise ReleaseVerificationError("release manifest SHA-256 differs")
    try:
        manifest = _load_strict_object(encoded, "release manifest")
        release = identity_from_manifest(
            manifest,
            manifest_path=(
                str(manifest_path.resolve().relative_to(root))
                if not overrides
                else f"config/step5d/releases/{manifest_sha256}/manifest.json"
            ),
            manifest_sha256=manifest_sha256,
        )
    except (ReleaseIdentityError, ValueError) as exc:
        raise ReleaseVerificationError(str(exc)) from exc

    payload_root = manifest_path.resolve().parent
    checked: dict[str, str] = {}
    if set(release.source_fingerprints) != REQUIRED_EXPERIMENT_SOURCE_FINGERPRINTS:
        raise ReleaseVerificationError("runtime source fingerprint coverage differs")
    for extension, reference in release.artifacts.items():
        path = _resolve(payload_root, str(reference["path"]), overrides)
        digest = _sha256(path)
        if digest != reference["sha256"]:
            raise ReleaseVerificationError(f"artifact SHA-256 differs: {extension}")
        checked[str(reference["path"])] = digest
    for role, references, base in (
        ("source", release.source_fingerprints, root),
        ("generated", release.generated_files, payload_root),
    ):
        for relative, expected in references.items():
            source_base = base
            if (
                role == "source"
                and relative not in overrides
                and (payload_root / relative).is_file()
            ):
                source_base = payload_root
            digest = _sha256(_resolve(source_base, relative, overrides))
            if digest != expected:
                raise ReleaseVerificationError(f"{role} fingerprint differs: {relative}")
            checked[relative] = digest
    safety_path = _resolve(
        payload_root, str(release.safety_envelope["path"]), overrides
    )
    if _sha256(safety_path) != release.safety_envelope["sha256"]:
        raise ReleaseVerificationError("safety envelope SHA-256 differs")
    repository_root = root
    for _ in range(release.verification["repository_source_root_depth"]):
        repository_root = repository_root.parent
    repository_root = repository_root.resolve(strict=True)
    repository_sources = release.verification["repository_source_fingerprints"]
    if set(repository_sources) != REQUIRED_REPOSITORY_SOURCE_FINGERPRINTS:
        raise ReleaseVerificationError("repository source fingerprint coverage differs")
    for relative, expected in repository_sources.items():
        path = (repository_root / relative).resolve()
        try:
            path.relative_to(repository_root)
        except ValueError as exc:
            raise ReleaseVerificationError(
                f"repository source path escapes root: {relative}"
            ) from exc
        digest = _sha256(path)
        if digest != expected:
            raise ReleaseVerificationError(
                f"repository source fingerprint differs: {relative}"
            )
        checked[f"@repository/{relative}"] = digest
    script_reference = release.artifacts[".script"]
    script_path = _resolve(payload_root, str(script_reference["path"]), overrides)
    script = script_path.read_text(encoding="utf-8")
    try:
        _, observed_runtime_identity = bind_final_script(
            script,
            program_id=release.program_id,
            protocol_id=release.protocol_id,
        )
    except RuntimeIdentityError as exc:
        raise ReleaseVerificationError(str(exc)) from exc
    if observed_runtime_identity != release.tp_runtime_identity:
        raise ReleaseVerificationError("TP runtime identity manifest binding differs")
    deploy_relative, _ = _single_generated(
        release, f"/{release.program_id}.deploy-manifest.json"
    )
    deploy_path = _resolve(payload_root, deploy_relative, overrides)
    deploy = _load_strict_object(deploy_path.read_bytes(), "TP deploy manifest")
    expected_deploy_artifacts = [
        {
            "filename": f"{release.program_id}{extension}",
            "source": f"{release.program_id}{extension}",
            "sha256": release.artifact_sha256[extension],
        }
        for extension in (".script", ".txt", ".urp")
    ]
    if any(
        (
            deploy.get("schema_version") != 2,
            deploy.get("basename") != release.program_id,
            deploy.get("controller_directory")
            != str(PurePosixPath(release.controller_target).parent),
            deploy.get("artifacts") != expected_deploy_artifacts,
            deploy.get("tp_runtime_identity") != release.tp_runtime_identity,
        )
    ):
        raise ReleaseVerificationError("TP deploy manifest release binding differs")
    input_registers = _registers(r"read_input_integer_register\(\s*(\d+)\s*\)", script)
    output_registers = _registers(r"write_output_integer_register\(\s*(\d+)\s*,", script)
    if input_registers != set(range(24, 32)):
        raise ReleaseVerificationError("script input-register contract lacks exact 24..31 routing")
    if output_registers != set(range(24, 38)):
        raise ReleaseVerificationError(
            "script output-register contract lacks exact 24..37 routing"
        )
    state_write_order = _state_write_order(script)
    expected_state_write_order = (24, 25, 27, 28, 29, 31, 32, 33, 34, 26, 30)
    if state_write_order != expected_state_write_order:
        raise ReleaseVerificationError(
            "TP state publication must write identity, then state, then consumed-sequence commit"
        )
    state_body = _function_body(script, "codex_autotune_write_state")
    if (
        not state_body.startswith("  codex_step5d_publish_runtime_identity()\n")
        or state_body.count("codex_step5d_publish_runtime_identity()") != 1
    ):
        raise ReleaseVerificationError(
            "TP state publication must refresh runtime identity before state writes"
        )
    identity_write_order = tuple(
        int(value)
        for value in re.findall(
            r"write_output_integer_register\(\s*(\d+)\s*,",
            _function_body(script, "codex_step5d_publish_runtime_identity"),
        )
    )
    if identity_write_order != (35, 36, 37):
        raise ReleaseVerificationError("TP runtime identity write order differs")
    required_markers = (
        "read_input_integer_register(31)",
        "write_output_integer_register(34,",
        "write_output_integer_register(35, codex_step5d_runtime_protocol_version)",
        "write_output_integer_register(36, codex_step5d_runtime_digest_hi)",
        "write_output_integer_register(37, codex_step5d_runtime_digest_lo)",
        "codex_autotune_wait_for_arm(campaign_epoch, trial_id, 78",
        "next_command == 4",
        "codex_autotune_publish_state_and_halt(campaign_epoch, trial_id, 77",
        "movel(entry_xy_pose, a=0.135, v=0.090, r=0.0)",
        "movel(entry_precontact_pose, a=0.060, v=0.040, r=0.0)",
        "def codex_autotune_wait_for_arm(campaign_epoch, trial_id, state, candidate_token, terminal_reason, execution_profile_id, consumed_command_seq):",
        "  while True:",
    )
    if "while waiting_s <" in script:
        raise ReleaseVerificationError("script still contains bounded READY wait loop")
    if "candidate_token, 19, execution_profile_id" in script:
        raise ReleaseVerificationError("script still contains terminal reason 19")
    missing = [marker for marker in required_markers if marker not in script]
    if missing:
        raise ReleaseVerificationError(f"script rolling semantics are incomplete: {missing}")
    if script.count("movel(entry_xy_pose") != 1 or script.count(
        "movel(entry_precontact_pose"
    ) != 1:
        raise ReleaseVerificationError("script prealign return segments are not exact")

    urp_reference = release.artifacts[".urp"]
    urp_path = _resolve(payload_root, str(urp_reference["path"]), overrides)
    try:
        xml_root = ET.fromstring(gzip.decompress(urp_path.read_bytes()))
    except (OSError, ET.ParseError) as exc:
        raise ReleaseVerificationError(f"URP cachedContents is invalid: {exc}") from exc
    cached = None
    script_node = None
    for node in xml_root.iter():
        if node.tag == "cachedContents":
            cached = html.unescape(node.text or "")
        elif node.tag == "file" and node.attrib.get("resolves-to") == "file":
            script_node = node.text or ""
    if xml_root.attrib.get("name") != release.program_id:
        raise ReleaseVerificationError("URP program name differs")
    if cached != script:
        raise ReleaseVerificationError("URP cachedContents differs from script bytes")
    if not isinstance(script_node, str) or not script_node.endswith(
        f"/{release.program_id}.script"
    ):
        raise ReleaseVerificationError("URP script node differs from release program")

    from step5d_autotune_contract import ExecutionProfile
    from step5d_runtime_codec import execution_profile_integer_id

    normal_rate, expected_profile_integer_id = (
        ROLLING_EXECUTION_PROFILE_BINDINGS[release.execution_profile_id]
    )
    high_dynamics = normal_rate >= 0.5
    profile = ExecutionProfile(
        release.execution_profile_id,
        normal_rate,
        2.5 if high_dynamics else 0.5,
        2.5 if high_dynamics else 0.5,
        qdot_cap_rad_s=2.5 if high_dynamics else 0.5,
        bridge_angular_limit_rad_s=0.25 if high_dynamics else 0.05,
    )
    encoded_profile = execution_profile_integer_id(profile)
    if encoded_profile != expected_profile_integer_id:
        raise ReleaseVerificationError(
            "host execution-profile codec does not round-trip the release profile"
        )
    return {
        "ok": True,
        "manifest_sha256": manifest_sha256,
        "program_id": release.program_id,
        "protocol_id": release.protocol_id,
        "input_integer_registers": sorted(input_registers),
        "output_integer_registers": sorted(output_registers),
        "state_write_order": list(state_write_order),
        "runtime_identity_write_order": list(identity_write_order),
        "tp_runtime_identity": dict(release.tp_runtime_identity),
        "execution_profile_integer_id": encoded_profile,
        "checked_file_sha256": dict(sorted(checked.items())),
    }


__all__ = [
    "ReleaseVerificationError",
    "verify_release_manifest",
]
