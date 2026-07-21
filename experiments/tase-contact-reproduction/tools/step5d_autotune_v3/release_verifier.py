"""Independent byte-level verifier for an immutable Step5d rolling release."""

from __future__ import annotations

import gzip
import hashlib
import html
import re
from pathlib import Path
from typing import Any, Mapping
import xml.etree.ElementTree as ET

from .release_identity import (
    ROLLING_EXECUTION_PROFILE_INTEGER_ID,
    ROLLING_NORMAL_MAX_RATE_RAD_S,
    ReleaseIdentity,
    ReleaseIdentityError,
    identity_from_manifest,
)
from .release_identity import _strict_object as _load_strict_object


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

    checked: dict[str, str] = {}
    for extension, reference in release.artifacts.items():
        path = _resolve(root, str(reference["path"]), overrides)
        digest = _sha256(path)
        if digest != reference["sha256"]:
            raise ReleaseVerificationError(f"artifact SHA-256 differs: {extension}")
        checked[str(reference["path"])] = digest
    for role, references in (
        ("source", release.source_fingerprints),
        ("generated", release.generated_files),
        ("mirror", release.compatibility_mirrors),
    ):
        for relative, expected in references.items():
            digest = _sha256(_resolve(root, relative, overrides))
            if digest != expected:
                raise ReleaseVerificationError(f"{role} fingerprint differs: {relative}")
            checked[relative] = digest
    readback_path = _resolve(root, str(release.controller_readback["path"]), overrides)
    if _sha256(readback_path) != release.controller_readback["sha256"]:
        raise ReleaseVerificationError("fresh controller readback manifest SHA-256 differs")
    readback = _load_strict_object(readback_path.read_bytes(), "controller readback")
    if any(
        (
            readback.get("status") != "controller read-back verified",
            readback.get("verified") is not True,
            readback.get("program") != release.program_id,
            readback.get("triplet_sha256") != release.artifact_sha256,
        )
    ):
        raise ReleaseVerificationError("controller readback identity or triplet differs")

    script_reference = release.artifacts[".script"]
    script_path = _resolve(root, str(script_reference["path"]), overrides)
    script = script_path.read_text(encoding="utf-8")
    input_registers = _registers(r"read_input_integer_register\(\s*(\d+)\s*\)", script)
    output_registers = _registers(r"write_output_integer_register\(\s*(\d+)\s*,", script)
    if 31 not in input_registers or not input_registers.issubset(set(range(24, 32))):
        raise ReleaseVerificationError("script input-register contract lacks exact 24..31 routing")
    if 34 not in output_registers or not output_registers.issubset(set(range(24, 35))):
        raise ReleaseVerificationError("script output-register contract lacks exact 24..34 routing")
    required_markers = (
        "read_input_integer_register(31)",
        "write_output_integer_register(34,",
        "codex_autotune_wait_for_arm(campaign_epoch, trial_id, 78",
        "next_command == 4",
        "codex_autotune_publish_state_and_halt(campaign_epoch, trial_id, 77",
        "movel(entry_xy_pose, a=0.135, v=0.090, r=0.0)",
        "movel(entry_precontact_pose, a=0.060, v=0.040, r=0.0)",
        "while waiting_s < 30.000",
    )
    missing = [marker for marker in required_markers if marker not in script]
    if missing:
        raise ReleaseVerificationError(f"script rolling semantics are incomplete: {missing}")
    if script.count("movel(entry_xy_pose") != 1 or script.count(
        "movel(entry_precontact_pose"
    ) != 1:
        raise ReleaseVerificationError("script prealign return segments are not exact")

    urp_reference = release.artifacts[".urp"]
    urp_path = _resolve(root, str(urp_reference["path"]), overrides)
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
    from step5d_autotune_supervisor import execution_profile_integer_id

    profile = ExecutionProfile(
        release.execution_profile_id,
        ROLLING_NORMAL_MAX_RATE_RAD_S,
        0.5,
        0.5,
    )
    encoded_profile = execution_profile_integer_id(profile)
    if encoded_profile != ROLLING_EXECUTION_PROFILE_INTEGER_ID:
        raise ReleaseVerificationError("host execution-profile codec does not round-trip 633")
    return {
        "ok": True,
        "manifest_sha256": manifest_sha256,
        "program_id": release.program_id,
        "protocol_id": release.protocol_id,
        "input_integer_registers": sorted(input_registers),
        "output_integer_registers": sorted(output_registers),
        "execution_profile_integer_id": encoded_profile,
        "checked_file_sha256": dict(sorted(checked.items())),
    }


__all__ = [
    "ReleaseVerificationError",
    "verify_release_manifest",
]
