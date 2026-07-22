"""Immutable identity helpers for the isolated Step5d manual NO_ARM bridge."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

import promote_step5d_manual_release as manual_release
from step5d_manual_atomic_release import canonical_bytes
from step5d_manual_profile import load_manual_launch_profile as load_launch_profile


ROOT = Path(__file__).resolve().parents[1]
CONTEXT_SCHEMA = "step5d.manual-hold/bridge-start-context-v1"
PREFLIGHT_SCHEMA = "step5d.manual-hold/live-preflight-v1"
TICKET_SCHEMA = "step5d.manual-hold/runtime-ticket-v1"
PROGRAM = manual_release.PROGRAM
PROTOCOL = manual_release.PROTOCOL
WIRE_PROTOCOL = "v3_full_home_rolling_arm_v1"
CONTROL_PROFILE = "step5d_strict_rnn_autotune_v1"
RELEASE_STAGE = "step5d_strict_rnn_autotune_v3"
DEFAULT_CONTEXT_MAX_AGE_S = 900.0
DEFAULT_PREFLIGHT_MAX_AGE_S = 30.0
CANONICAL_SHELL = ROOT / "scripts/step5d-autotune-v3.sh"


class ManualBridgeError(RuntimeError):
    pass


def require_canonical_shell() -> None:
    launcher = os.environ.get("STEP5D_V3_CANONICAL_LAUNCHER", "")
    shell_pid = os.environ.get("STEP5D_V3_SHELL_PID", "")
    try:
        shell_pid_value = int(shell_pid)
        launcher_path = Path(launcher).resolve(strict=True)
    except (OSError, TypeError, ValueError) as exc:
        raise ManualBridgeError(
            "internal Manual V2 runner requires scripts/step5d-autotune-v3.sh bridge"
        ) from exc
    if launcher_path != CANONICAL_SHELL.resolve(strict=True) or os.getppid() != shell_pid_value:
        raise ManualBridgeError(
            "internal Manual V2 runner requires scripts/step5d-autotune-v3.sh bridge"
        )


def sha256_path(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ManualBridgeError(f"required regular file is missing: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def strict_object(path: Path, role: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ManualBridgeError(f"{role} must be a real regular file")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ManualBridgeError(f"{role} repeats JSON key {key!r}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ManualBridgeError(f"{role} contains non-finite {value}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ManualBridgeError(f"{role} is invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ManualBridgeError(f"{role} must be a JSON object")
    return payload


def require_fresh_timestamp(
    value: Any,
    *,
    role: str,
    max_age_s: float,
    now: datetime | None = None,
) -> None:
    try:
        created = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ManualBridgeError(f"{role} timestamp differs") from exc
    current = now or datetime.now(timezone.utc)
    age_s = (current - created).total_seconds()
    if max_age_s <= 0.0 or not -1.0 <= age_s <= max_age_s:
        raise ManualBridgeError(f"{role} is stale")


def _release_document(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    verified = manual_release.load_manual_release(root)
    manifest = strict_object(root / verified["manifest_path"], "manual release manifest")
    return verified, manifest


def _source_surface_sha256(manifest: Mapping[str, Any]) -> str:
    sources = manifest.get("source_fingerprints")
    if not isinstance(sources, Mapping) or not sources:
        raise ManualBridgeError("manual release source fingerprints are missing")
    return hashlib.sha256(canonical_bytes(dict(sources))).hexdigest()


def build_context(
    root: Path,
    *,
    plant_epoch: int,
    launch_profile_path: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = root.resolve(strict=True)
    if isinstance(plant_epoch, bool) or not isinstance(plant_epoch, int) or plant_epoch < 1:
        raise ManualBridgeError("plant_epoch must be a positive integer")
    verified, manifest = _release_document(root)
    launch_path = launch_profile_path.expanduser().resolve(strict=True)
    launch_path.relative_to(root)
    launch = load_launch_profile(launch_path)
    created = now or datetime.now(timezone.utc)
    if created.tzinfo is None or created.utcoffset() is None:
        raise ManualBridgeError("manual bridge context timestamp must be timezone-aware")
    payload = {
        "schema": CONTEXT_SCHEMA,
        "program": PROGRAM,
        "protocol": PROTOCOL,
        "wire_protocol": WIRE_PROTOCOL,
        "control_profile_id": CONTROL_PROFILE,
        "release_stage_id": RELEASE_STAGE,
        "manual_release_manifest_sha256": verified["manifest_sha256"],
        "parent_r009_commit": manifest["identity"]["parent_r009_commit"],
        "parent_r009_release_manifest_sha256": manifest["identity"][
            "parent_r009_release_manifest_sha256"
        ],
        "triplet_sha256": {
            extension: reference["sha256"]
            for extension, reference in manifest["artifacts"].items()
        },
        "source_surface_sha256": _source_surface_sha256(manifest),
        "launch_profile": {
            "path": launch_path.relative_to(root).as_posix(),
            "sha256": sha256_path(launch_path),
            "fingerprint": launch.fingerprint,
            "manual_program_override": PROGRAM,
        },
        "plant_epoch": plant_epoch,
        "created_at": created.isoformat(),
        "valid_for_s": DEFAULT_CONTEXT_MAX_AGE_S,
        "bridge_authorized": True,
        "arm_authorized": False,
        "motion_authorized": False,
    }
    return {**payload, "context_sha256": hashlib.sha256(canonical_bytes(payload)).hexdigest()}


def load_context(
    root: Path,
    path: Path,
    *,
    max_age_s: float = DEFAULT_CONTEXT_MAX_AGE_S,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = root.resolve(strict=True)
    payload = strict_object(path.expanduser().absolute(), "manual bridge-start context")
    expected_fields = {
        "schema", "program", "protocol", "wire_protocol", "control_profile_id",
        "release_stage_id", "manual_release_manifest_sha256", "parent_r009_commit",
        "parent_r009_release_manifest_sha256", "triplet_sha256",
        "source_surface_sha256", "launch_profile", "plant_epoch", "created_at",
        "valid_for_s", "bridge_authorized", "arm_authorized", "motion_authorized",
        "context_sha256",
    }
    if set(payload) != expected_fields:
        raise ManualBridgeError("manual bridge-start context fields differ")
    material = dict(payload)
    observed_digest = material.pop("context_sha256")
    if observed_digest != hashlib.sha256(canonical_bytes(material)).hexdigest():
        raise ManualBridgeError("manual bridge-start context digest differs")
    verified, manifest = _release_document(root)
    fixed = {
        "schema": CONTEXT_SCHEMA,
        "program": PROGRAM,
        "protocol": PROTOCOL,
        "wire_protocol": WIRE_PROTOCOL,
        "control_profile_id": CONTROL_PROFILE,
        "release_stage_id": RELEASE_STAGE,
        "manual_release_manifest_sha256": verified["manifest_sha256"],
        "parent_r009_commit": manifest["identity"]["parent_r009_commit"],
        "parent_r009_release_manifest_sha256": manifest["identity"][
            "parent_r009_release_manifest_sha256"
        ],
        "source_surface_sha256": _source_surface_sha256(manifest),
        "bridge_authorized": True,
        "arm_authorized": False,
        "motion_authorized": False,
    }
    for key, value in fixed.items():
        if payload.get(key) != value:
            raise ManualBridgeError(f"manual bridge-start context {key} differs")
    expected_triplet = {
        extension: reference["sha256"]
        for extension, reference in manifest["artifacts"].items()
    }
    if payload["triplet_sha256"] != expected_triplet:
        raise ManualBridgeError("manual bridge-start triplet differs")
    launch_ref = payload["launch_profile"]
    if not isinstance(launch_ref, Mapping) or set(launch_ref) != {
        "path", "sha256", "fingerprint", "manual_program_override"
    }:
        raise ManualBridgeError("manual bridge launch-profile reference differs")
    launch_path = root / str(launch_ref["path"])
    launch = load_launch_profile(launch_path)
    if any(
        (
            sha256_path(launch_path) != launch_ref["sha256"],
            launch.fingerprint != launch_ref["fingerprint"],
            launch_ref["manual_program_override"] != PROGRAM,
        )
    ):
        raise ManualBridgeError("manual bridge launch profile drifted")
    require_fresh_timestamp(
        payload["created_at"],
        role="manual bridge-start context",
        max_age_s=min(max_age_s, payload["valid_for_s"]),
        now=now,
    )
    return payload


def write_once(path: Path, payload: Mapping[str, Any]) -> None:
    destination = path.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ManualBridgeError("manual bridge-start context output already exists")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False).encode() + b"\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary_path, destination)
    except FileExistsError as exc:
        raise ManualBridgeError("manual bridge-start context output already exists") from exc
    finally:
        temporary_path.unlink(missing_ok=True)
