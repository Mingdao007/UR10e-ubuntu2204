#!/usr/bin/python3.10
"""Promote one fresh r012 GET into an immutable rolling-v1 release.

This owner is filesystem-only.  The caller performs upload/readback first; this
module never loads or starts a TP program, opens a bridge, sends ARM, or moves
the robot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping

from step5d_autotune_v3.atomic_release import (
    AtomicReleasePublisher,
    PendingRelease,
    canonical_bytes,
)
from step5d_autotune_v3.release_identity import (
    ACTIVE_TP_PROGRAM_ID,
    CONTROL_PROFILE_ID,
    RELEASE_MANIFEST_SCHEMA,
    RELEASE_STAGE_ID,
    REQUIRED_EXPERIMENT_SOURCE_FINGERPRINTS,
    REQUIRED_REPOSITORY_SOURCE_FINGERPRINTS,
    ROLLING_EXECUTION_PROFILE_ID,
    ROLLING_EXECUTION_PROFILE_INTEGER_ID,
    ROLLING_NORMAL_MAX_RATE_RAD_S,
    ROLLING_PROTOCOL,
    SAFETY_ENVELOPE_PATH,
    ReleaseIdentityError,
    load_current_release,
    load_current_release_for_compatible_readback,
    release_payload_path,
    release_runtime_environment_binding,
)
from step5d_autotune_v3.release_verifier import verify_release_manifest
from step5d_autotune_v3.profile import contract_sha256
from step5d_autotune_v3.runtime_identity import RuntimeIdentityError, bind_final_script
from step5d_autotune_v3.state import atomic_json


ROOT = Path(__file__).resolve().parents[1]
PROGRAM = ACTIVE_TP_PROGRAM_ID
TARGET_DIR = "/programs/andyl/kunwei/step5"
PACKAGE_DIR = Path("programs/step5/step5d")
EXTENSIONS = (".script", ".txt", ".urp")
READBACK = Path("config/step5d_autotune_controller_readback_v3.json")
RAW_READBACK = Path(
    "config/step5d/manifests/step5d_strict_rnn_autotune_v3_r012/"
    "controller_readback.json"
)
LOCAL_CANDIDATE = Path(
    "config/step5d/manifests/step5d_strict_rnn_autotune_v3_r012/"
    "local_candidate.json"
)
MANUAL_LAUNCH_PROFILE = Path("config/step5d/manual/launch_profile.json")
MANUAL_PROFILE_TP_PROGRAM = "step5d_strict_rnn_autotune_v3_r009"
STATIC_PROJECTIONS = (
    Path("config/current_stage.json"),
    Path("config/step5_stage_table.json"),
    Path("config/tase_protocol_table.json"),
    Path("config/step5d/v3_active_surface.json"),
    Path("config/step5/step5d_autotune_v3_control_contract.json"),
    Path("config/step5/step5d_autotune_v3_launch_profile.json"),
    MANUAL_LAUNCH_PROFILE,
)
SOURCE_INPUTS = tuple(
    Path(relative) for relative in sorted(REQUIRED_EXPERIMENT_SOURCE_FINGERPRINTS)
)
REPOSITORY_SOURCE_INPUTS = tuple(
    Path(relative) for relative in sorted(REQUIRED_REPOSITORY_SOURCE_FINGERPRINTS)
)
STATIC_PROJECTION_SHA256 = {
    "config/tase_protocol_table.json": "26552485d5260bdabe2264628d3be0815a7f686c2165850c87bb68194ac354bb",
    "config/step5d/v3_active_surface.json": "a9946b39371530568af48189ca2f1a0190228f678c7c9cd010b94d525aacff6b",
}
CONTRACT_STATIC_SHA256 = "5bbc7fa620a1f945f72ca6742a0b8fdc4cd4149c278e959e0760cffe167d2088"
LAUNCH_STATIC_SHA256 = "d094cedd3813b938ff310e85c0f4f0d0dbc82f2c1ed831713648f3c1ece80202"
PENDING = PendingRelease(
    program_id=PROGRAM,
    protocol_id=ROLLING_PROTOCOL,
    normal_max_rate_rad_s=ROLLING_NORMAL_MAX_RATE_RAD_S,
    execution_profile_id=ROLLING_EXECUTION_PROFILE_ID,
    execution_profile_integer_id=ROLLING_EXECUTION_PROFILE_INTEGER_ID,
)
_TRANSACTION = re.compile(r"^[0-9a-f]{32}$")


class R009PromotionError(RuntimeError):
    pass


def _sha256_bytes(encoded: bytes) -> str:
    return hashlib.sha256(encoded).hexdigest()


def _sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise R009PromotionError(f"required regular file is missing: {path}")
    return _sha256_bytes(path.read_bytes())


def _load(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise R009PromotionError(f"required regular JSON file is missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise R009PromotionError(f"invalid JSON at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise R009PromotionError(f"JSON object required: {path}")
    return payload


def _pretty(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _static_projection(root: Path, relative: Path) -> dict[str, Any]:
    """Load one SHA-pinned canonical static input; drift has no fallback path."""

    expected = STATIC_PROJECTION_SHA256.get(relative.as_posix())
    if expected is None or _sha256(root / relative) != expected:
        raise R009PromotionError(
            f"canonical static projection input drifted: {relative.as_posix()}"
        )
    return _load(root / relative)


def _object(value: Any, role: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise R009PromotionError(f"canonical {role} object is missing")
    return value


def _require_keys(value: Mapping[str, Any], required: set[str], role: str) -> None:
    missing = sorted(required - set(value))
    if missing:
        raise R009PromotionError(f"canonical {role} fields are missing: {missing}")


def _render_current_stage(
    source: Mapping[str, Any],
    *,
    triplet_sha256: Mapping[str, str],
    deploy_manifest_sha256: str,
    numeric_sanity_sha256: str,
    readback_sha256: str,
) -> dict[str, Any]:
    """Rewrite only selected-release truth; retained historical evidence stays frozen."""

    base = json.loads(json.dumps(source, allow_nan=False))
    _require_keys(
        base,
        {
            "controller_readback_manifest",
            "controller_readback_manifest_sha256",
            "controller_script",
            "controller_target",
            "delivery_manifest",
            "evidence",
            "local_candidate",
            "local_triplet",
            "program",
            "sha256",
            "status",
        },
        "current-stage selected release",
    )
    local_triplet = (PACKAGE_DIR / PROGRAM).as_posix()
    controller_target = str(PurePosixPath(TARGET_DIR) / f"{PROGRAM}.urp")
    controller_script = str(PurePosixPath(TARGET_DIR) / f"{PROGRAM}.script")
    base.update(
        {
            "controller_readback_manifest": READBACK.as_posix(),
            "controller_readback_manifest_sha256": readback_sha256,
            "controller_readback_verified_for_selected_triplet": True,
            "controller_script": controller_script,
            "controller_target": controller_target,
            "delivery_manifest": READBACK.as_posix(),
            "local_triplet": local_triplet,
            "sha256": dict(triplet_sha256),
            "status": "step5d_autotune_v3_r012_controller_readback_verified",
        }
    )
    evidence = _object(base.get("evidence"), "current-stage evidence")
    evidence["sha256"] = dict(triplet_sha256)
    local_candidate = _object(
        base.get("local_candidate"), "current-stage local candidate"
    )
    local_candidate.update(
        {
            "controller_readback_verified": True,
            "controller_uploaded": True,
            "disposition": "controller_readback_verified_promoted_current",
            "deploy_manifest_sha256": deploy_manifest_sha256,
            "manifest": LOCAL_CANDIDATE.as_posix(),
            "numeric_sanity_sha256": numeric_sanity_sha256,
            "program": PROGRAM,
            "triplet_sha256": dict(triplet_sha256),
        }
    )
    for field in (
        "bridge_trigger",
        "execution_state",
        "live_run_status",
        "liveprep_status",
        "readiness",
    ):
        base.pop(field, None)
    return base


def _render_stage_table(
    source: Mapping[str, Any],
    *,
    triplet_sha256: Mapping[str, str],
    deploy_manifest_sha256: str,
    numeric_sanity_sha256: str,
    readback_sha256: str,
) -> dict[str, Any]:
    base = json.loads(json.dumps(source, allow_nan=False))
    stages = base.get("stages")
    if not isinstance(stages, list):
        raise R009PromotionError("canonical stage-table rows are missing")
    rows = [
        row
        for row in stages
        if isinstance(row, dict) and row.get("id") == RELEASE_STAGE_ID
    ]
    if len(rows) != 1:
        raise R009PromotionError("canonical selected stage-table row is not unique")
    row = rows[0]
    _require_keys(
        row,
        {"current_binding", "operator_lifecycle", "package_delivery"},
        "selected stage-table row",
    )
    row.pop("execution_readiness", None)
    local_triplet = (PACKAGE_DIR / PROGRAM).as_posix()
    controller_target = str(PurePosixPath(TARGET_DIR) / f"{PROGRAM}.urp")
    current_binding = _object(row.get("current_binding"), "stage current binding")
    current_binding["controller_target"] = controller_target
    current_binding.pop("live_authorized", None)
    operator = _object(row.get("operator_lifecycle"), "stage operator lifecycle")
    operator["expected_program"] = controller_target
    operator.pop("live_readiness_state", None)
    package = _object(row.get("package_delivery"), "stage package delivery")
    _require_keys(
        package,
        {
            "controller_readback_manifest",
            "controller_readback_manifest_sha256",
            "controller_target",
            "local_candidate",
            "local_triplet",
            "program_basename",
            "sha256",
            "tp_fingerprint",
        },
        "stage package delivery",
    )
    package.update(
        {
            "controller_readback_manifest": READBACK.as_posix(),
            "controller_readback_manifest_sha256": readback_sha256,
            "controller_readback_verified": True,
            "controller_target": controller_target,
            "fresh_controller_sha_at": None,
            "local_triplet": local_triplet,
            "program_basename": PROGRAM,
            "sha256": dict(triplet_sha256),
            "status": "controller_readback_verified",
            "tp_fingerprint": deploy_manifest_sha256,
        }
    )
    local_candidate = _object(
        package.get("local_candidate"), "stage package local candidate"
    )
    local_candidate.update(
        {
            "controller_readback_verified": True,
            "controller_uploaded": True,
            "deploy_manifest_sha256": deploy_manifest_sha256,
            "local_triplet": local_triplet,
            "numeric_sanity_sha256": numeric_sanity_sha256,
            "program_basename": PROGRAM,
            "sha256": dict(triplet_sha256),
            "status": "controller_readback_verified",
        }
    )
    return base


def _render_contract(
    root: Path,
    source: Mapping[str, Any],
    *,
    triplet_sha256: Mapping[str, str],
    deploy_manifest_sha256: str,
    numeric_sanity_sha256: str,
    readback_sha256: str,
) -> dict[str, Any]:
    base = json.loads(json.dumps(source, allow_nan=False))
    dynamic_fields = {
        "source_sha256",
        "candidate_tp_artifact_sha256",
        "tp_artifact_sha256",
        "candidate_tp_identity",
        "deployment_tp_identity",
        "promotion_status",
    }
    static = {key: value for key, value in base.items() if key not in dynamic_fields}
    if _sha256_bytes(canonical_bytes(static)) != CONTRACT_STATIC_SHA256:
        raise R009PromotionError("canonical control-contract static input drifted")
    source_sha256 = {
        relative: _sha256(root / relative)
        for relative in base["source_sha256"]
    }
    return {
        **static,
        "source_sha256": source_sha256,
        "candidate_tp_artifact_sha256": dict(triplet_sha256),
        "tp_artifact_sha256": dict(triplet_sha256),
        "candidate_tp_identity": {
            "program": PROGRAM,
            "mode": "controller_readback_verified_promoted_current",
            "artifact_dir": PACKAGE_DIR.as_posix(),
            "deploy_manifest_sha256": deploy_manifest_sha256,
            "numeric_sanity_sha256": numeric_sanity_sha256,
        },
        "deployment_tp_identity": {
            "program": PROGRAM,
            "mode": "explicit_v3_identity_precontact_pose_frozen_v1_control",
            "artifact_dir": PACKAGE_DIR.as_posix(),
            "readback_manifest": READBACK.as_posix(),
            "readback_manifest_sha256": readback_sha256,
            "tp_fingerprint": deploy_manifest_sha256,
        },
        "promotion_status": "controller_readback_verified",
    }


def _render_launch_profile(
    source: Mapping[str, Any],
    *,
    contract_sha256: str,
    tp_program_id: str = PROGRAM,
) -> dict[str, Any]:
    base = json.loads(json.dumps(source, allow_nan=False))
    dynamic_fields = {
        "release_stage_id",
        "control_profile_id",
        "tp_program_id",
        "control_contract_sha256",
    }
    static = {key: value for key, value in base.items() if key not in dynamic_fields}
    if _sha256_bytes(canonical_bytes(static)) != LAUNCH_STATIC_SHA256:
        raise R009PromotionError("canonical launch-profile static input drifted")
    return {
        **static,
        "release_stage_id": RELEASE_STAGE_ID,
        "control_profile_id": CONTROL_PROFILE_ID,
        "tp_program_id": tp_program_id,
        "control_contract_sha256": contract_sha256,
    }


def _artifact_paths(artifact_dir: Path) -> dict[str, Path]:
    return {
        extension: artifact_dir / f"{PROGRAM}{extension}"
        for extension in EXTENSIONS
    }


def current_release_artifact_dir(root: Path) -> Path:
    """Reuse manifest-bound TP bytes when compatibility mirrors are absent."""

    root = root.resolve(strict=True)
    try:
        release = load_current_release_for_compatible_readback(root)
        references = {
            extension: release_payload_path(
                root,
                release,
                str(release.artifacts[extension]["path"]),
            )
            for extension in EXTENSIONS
        }
        artifact_dir = next(iter(references.values())).parent
        if any(path.parent != artifact_dir for path in references.values()):
            raise R009PromotionError(
                "current release TP artifacts do not share a directory"
            )
        for suffix in (".deploy-manifest.json", ".numeric-sanity.json"):
            bundle_root = (root / release.manifest_path).parent
            release_payload_path(
                root,
                release,
                (
                    artifact_dir.relative_to(bundle_root) / f"{PROGRAM}{suffix}"
                ).as_posix(),
            )
    except (KeyError, ReleaseIdentityError) as exc:
        raise R009PromotionError(
            f"current immutable TP artifacts are unavailable: {exc}"
        ) from exc
    return artifact_dir


def default_release_artifact_dir(root: Path) -> Path:
    root = root.resolve(strict=True)
    canonical = root / PACKAGE_DIR
    required = (
        *_artifact_paths(canonical).values(),
        canonical / f"{PROGRAM}.deploy-manifest.json",
        canonical / f"{PROGRAM}.numeric-sanity.json",
    )
    if all(
        not path.is_symlink() and path.is_file()
        for path in required
    ):
        return canonical
    return current_release_artifact_dir(root)


def validate_local_candidate(
    root: Path,
    artifact_dir: Path,
) -> tuple[Path, dict[str, str]]:
    """Validate local candidate bytes without asserting controller delivery."""

    root = root.resolve(strict=True)
    unresolved_artifacts = artifact_dir.expanduser()
    if unresolved_artifacts.is_symlink():
        raise R009PromotionError("pending artifact directory is unsafe")
    resolved_artifacts = unresolved_artifacts.resolve(strict=True)
    try:
        resolved_artifacts.relative_to(root)
    except ValueError as exc:
        raise R009PromotionError("pending artifact directory escapes experiment root") from exc
    if resolved_artifacts.is_symlink() or not resolved_artifacts.is_dir():
        raise R009PromotionError("pending artifact directory is unsafe")
    local_sha = {
        extension: _sha256(path)
        for extension, path in _artifact_paths(resolved_artifacts).items()
    }
    return resolved_artifacts, local_sha


def validate_delivery(
    root: Path,
    manifest_path: Path,
    artifact_dir: Path,
    *,
    expected_transaction_id: str | None = None,
    expected_manifest_sha256: str | None = None,
    expected_delivery_basis: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    root = root.resolve(strict=True)
    unresolved_manifest = manifest_path.expanduser()
    if unresolved_manifest.is_symlink():
        raise R009PromotionError("delivery manifest handoff is unsafe")
    manifest_path = unresolved_manifest.resolve(strict=True)
    try:
        manifest_path.relative_to(root)
    except ValueError as exc:
        raise R009PromotionError("delivery manifest escapes experiment root") from exc
    manifest = _load(manifest_path)
    if (
        expected_manifest_sha256 is not None
        and _sha256(manifest_path) != expected_manifest_sha256
    ):
        raise R009PromotionError("delivery manifest handoff SHA-256 differs")
    unresolved_artifacts = artifact_dir.expanduser()
    if unresolved_artifacts.is_symlink():
        raise R009PromotionError("pending artifact directory is unsafe")
    artifact_dir = unresolved_artifacts.resolve(strict=True)
    try:
        artifact_dir.relative_to(root)
    except ValueError as exc:
        raise R009PromotionError("pending artifact directory escapes experiment root") from exc
    if artifact_dir.is_symlink() or not artifact_dir.is_dir():
        raise R009PromotionError("pending artifact directory is unsafe")
    files = _artifact_paths(artifact_dir)
    local_sha = {extension: _sha256(path) for extension, path in files.items()}
    validation = manifest.get("validation")
    hashes = manifest.get("sha256")
    transaction = manifest.get("upload_transaction_id")
    if not isinstance(validation, Mapping) or not isinstance(hashes, Mapping):
        raise R009PromotionError("delivery validation or SHA closure is missing")
    if any(
        (
            manifest.get("status") != "controller read-back verified",
            manifest.get("delivery_mode")
            not in {
                "full_upload_readback",
                "existing_program_fresh_readback",
            },
            manifest.get("readback_source") != "fresh_controller_get",
            manifest.get("fresh_controller_sha_verified") is not True,
            manifest.get("target_dir") != TARGET_DIR,
            validation.get("program") != PROGRAM,
            validation.get("target_dir") != TARGET_DIR,
            validation.get("script_node_path")
            != str(PurePosixPath(TARGET_DIR) / f"{PROGRAM}.script"),
            not isinstance(transaction, str),
            _TRANSACTION.fullmatch(transaction or "") is None,
            expected_transaction_id is not None
            and transaction != expected_transaction_id,
        )
    ):
        raise R009PromotionError("delivery identity, target, or freshness differs")
    if not (
        hashes.get("local")
        == hashes.get("controller")
        == hashes.get("readback")
        == local_sha
    ):
        raise R009PromotionError("local/controller/readback triplet SHA closure differs")
    if manifest.get("delivery_mode") == "existing_program_fresh_readback":
        if (
            expected_delivery_basis is None
            or manifest.get("delivery_basis") != dict(expected_delivery_basis)
        ):
            raise R009PromotionError(
                "existing-program delivery basis reference differs"
            )
    elif expected_delivery_basis is not None:
        raise R009PromotionError(
            "delivery basis is valid only for existing-program adoption"
        )
    for extension, key in {
        ".script": "script_sha256",
        ".txt": "txt_sha256",
        ".urp": "urp_sha256",
    }.items():
        if validation.get(key) != local_sha[extension]:
            raise R009PromotionError(f"delivery validation SHA differs: {extension}")
        fetched = manifest_path.parent / f"{PROGRAM}{extension}"
        if _sha256(fetched) != local_sha[extension]:
            raise R009PromotionError(f"fresh GET bytes differ: {extension}")
    return manifest, local_sha


def _canonical_readback(
    *,
    triplet_sha256: Mapping[str, str],
    tp_fingerprint: str,
) -> dict[str, Any]:
    return {
        "schema": "step5d.autotune.controller-readback/v3",
        "status": "controller read-back verified",
        "verified": True,
        "program": PROGRAM,
        "control_profile_id": CONTROL_PROFILE_ID,
        "controller_target": str(PurePosixPath(TARGET_DIR) / f"{PROGRAM}.urp"),
        "triplet_sha256": dict(triplet_sha256),
        "tp_fingerprint": tp_fingerprint,
        "fresh_get_evidence": "per_campaign_delivery_observation",
        "safety_boundary": [
            "controller package upload and fresh GET only",
            "no Load or Play",
            "no bridge, ARM, contact, or motion",
        ],
    }


def _local_candidate(
    *,
    triplet_sha256: Mapping[str, str],
    deploy_manifest_sha256: str,
    numeric_sanity_sha256: str,
    readback_sha256: str,
) -> dict[str, Any]:
    return {
        "schema": "step5d.autotune-v3/local-tp-candidate-v2",
        "program": PROGRAM,
        "release_stage_id": RELEASE_STAGE_ID,
        "control_profile_id": CONTROL_PROFILE_ID,
        "protocol": PENDING.protocol_id,
        "normal_max_rate_rad_s": PENDING.normal_max_rate_rad_s,
        "execution_profile_id": PENDING.execution_profile_id,
        "execution_profile_integer_id": PENDING.execution_profile_integer_id,
        "disposition": "controller_readback_verified_pending_atomic_pointer",
        "controller_uploaded": True,
        "controller_readback_verified": True,
        "triplet_sha256": dict(triplet_sha256),
        "deploy_manifest_sha256": deploy_manifest_sha256,
        "numeric_sanity_sha256": numeric_sanity_sha256,
        "controller_readback_sha256": readback_sha256,
    }


def _compose_local_release(
    root: Path,
    artifact_dir: Path,
    triplet_sha: Mapping[str, str],
) -> tuple[dict[str, Any], dict[str, bytes], dict[str, str]]:
    root = root.resolve(strict=True)
    artifact_dir = artifact_dir.expanduser().resolve(strict=True)
    deploy = PACKAGE_DIR / f"{PROGRAM}.deploy-manifest.json"
    numeric = PACKAGE_DIR / f"{PROGRAM}.numeric-sanity.json"
    deploy_source = artifact_dir / f"{PROGRAM}.deploy-manifest.json"
    numeric_source = artifact_dir / f"{PROGRAM}.numeric-sanity.json"
    deploy_sha = _sha256(deploy_source)
    numeric_sha = _sha256(numeric_source)
    try:
        _, tp_runtime_identity = bind_final_script(
            (artifact_dir / f"{PROGRAM}.script").read_text(encoding="utf-8"),
            program_id=PROGRAM,
            protocol_id=ROLLING_PROTOCOL,
        )
    except (OSError, UnicodeError, RuntimeIdentityError) as exc:
        raise R009PromotionError(f"TP runtime identity verification failed: {exc}") from exc
    deploy_document = _load(deploy_source)
    expected_deploy_artifacts = [
        {
            "filename": f"{PROGRAM}{extension}",
            "source": f"{PROGRAM}{extension}",
            "sha256": triplet_sha[extension],
        }
        for extension in EXTENSIONS
    ]
    if (
        deploy_document.get("schema_version") != 2
        or deploy_document.get("basename") != PROGRAM
        or deploy_document.get("controller_directory") != TARGET_DIR
        or deploy_document.get("artifacts") != expected_deploy_artifacts
        or deploy_document.get("tp_runtime_identity") != tp_runtime_identity
        or tp_runtime_identity["script_artifact_sha256"] != triplet_sha[".script"]
    ):
        raise R009PromotionError("deploy manifest TP runtime identity binding differs")
    canonical_readback = _pretty(
        _canonical_readback(
            triplet_sha256=triplet_sha,
            tp_fingerprint=deploy_sha,
        )
    )
    readback_sha = _sha256_bytes(canonical_readback)
    local_candidate = _pretty(
        _local_candidate(
            triplet_sha256=triplet_sha,
            deploy_manifest_sha256=deploy_sha,
            numeric_sanity_sha256=numeric_sha,
            readback_sha256=readback_sha,
        )
    )

    bundle_files: dict[str, bytes] = {}
    for extension, path in _artifact_paths(artifact_dir).items():
        bundle_files[(PACKAGE_DIR / f"{PROGRAM}{extension}").as_posix()] = path.read_bytes()
    bundle_files[deploy.as_posix()] = deploy_source.read_bytes()
    bundle_files[numeric.as_posix()] = numeric_source.read_bytes()
    bundle_files[READBACK.as_posix()] = canonical_readback
    bundle_files[LOCAL_CANDIDATE.as_posix()] = local_candidate
    contract_relative = Path("config/step5/step5d_autotune_v3_control_contract.json")
    launch_relative = Path("config/step5/step5d_autotune_v3_launch_profile.json")
    contract_document = _render_contract(
        root,
        _load(root / contract_relative),
        triplet_sha256=triplet_sha,
        deploy_manifest_sha256=deploy_sha,
        numeric_sanity_sha256=numeric_sha,
        readback_sha256=readback_sha,
    )
    contract = _pretty(contract_document)
    contract_digest = _sha256_bytes(contract)
    bundle_files[contract_relative.as_posix()] = contract
    rendered_contract_sha256 = contract_sha256(contract_document)
    bundle_files[launch_relative.as_posix()] = _pretty(
        _render_launch_profile(
            _load(root / launch_relative),
            contract_sha256=rendered_contract_sha256,
        )
    )
    bundle_files[MANUAL_LAUNCH_PROFILE.as_posix()] = _pretty(
        _render_launch_profile(
            _load(root / MANUAL_LAUNCH_PROFILE),
            contract_sha256=rendered_contract_sha256,
            tp_program_id=MANUAL_PROFILE_TP_PROGRAM,
        )
    )
    for relative in STATIC_PROJECTIONS:
        if relative not in {
            contract_relative,
            launch_relative,
            MANUAL_LAUNCH_PROFILE,
        }:
            if relative == Path("config/current_stage.json"):
                projection = _render_current_stage(
                    _load(root / relative),
                    triplet_sha256=triplet_sha,
                    deploy_manifest_sha256=deploy_sha,
                    numeric_sanity_sha256=numeric_sha,
                    readback_sha256=readback_sha,
                )
            elif relative == Path("config/step5_stage_table.json"):
                projection = _render_stage_table(
                    _load(root / relative),
                    triplet_sha256=triplet_sha,
                    deploy_manifest_sha256=deploy_sha,
                    numeric_sanity_sha256=numeric_sha,
                    readback_sha256=readback_sha,
                )
            else:
                _static_projection(root, relative)
                bundle_files[relative.as_posix()] = (root / relative).read_bytes()
                continue
            bundle_files[relative.as_posix()] = _pretty(projection)

    generated_files = {
        relative: _sha256_bytes(bundle_files[relative])
        for relative in (
            deploy.as_posix(),
            numeric.as_posix(),
            LOCAL_CANDIDATE.as_posix(),
            launch_relative.as_posix(),
            MANUAL_LAUNCH_PROFILE.as_posix(),
        )
    }
    source_fingerprints = {
        relative.as_posix(): (
            _sha256_bytes(bundle_files[relative.as_posix()])
            if relative.as_posix() in bundle_files
            else _sha256(root / relative)
        )
        for relative in SOURCE_INPUTS
    }
    artifacts = {
        extension: {
            "path": (PACKAGE_DIR / f"{PROGRAM}{extension}").as_posix(),
            "sha256": digest,
        }
        for extension, digest in triplet_sha.items()
    }
    manifest: dict[str, Any] = {
        "schema": RELEASE_MANIFEST_SCHEMA,
        "identity": {
            "program_id": PENDING.program_id,
            "release_stage_id": RELEASE_STAGE_ID,
            "control_profile_id": CONTROL_PROFILE_ID,
            "protocol_id": PENDING.protocol_id,
            "normal_max_rate_rad_s": PENDING.normal_max_rate_rad_s,
            "execution_profile_id": PENDING.execution_profile_id,
            "execution_profile_integer_id": PENDING.execution_profile_integer_id,
        },
        "artifacts": artifacts,
        "controller_target": str(
            PurePosixPath(TARGET_DIR) / f"{PROGRAM}.urp"
        ),
        "tp_runtime_identity": tp_runtime_identity,
        "safety_envelope": {
            "path": SAFETY_ENVELOPE_PATH,
            "sha256": contract_digest,
        },
        "runtime_environment": release_runtime_environment_binding(
            source_fingerprints
        ),
        "source_fingerprints": source_fingerprints,
        "generated_files": generated_files,
        "verification": {
            "canonical_verifier": "independent_script_urp_v3",
            "staged_bytes_required": True,
            "pointer_switched_last": True,
            "runtime_identity_derivation": (
                "canonical_script_identity_basis_sha256_plus_"
                "final_artifact_sha256_v1"
            ),
            "repository_source_root_depth": 2,
            "repository_source_fingerprints": {
                relative.as_posix(): _sha256(root.parents[1] / relative)
                for relative in REPOSITORY_SOURCE_INPUTS
            },
        },
    }
    targets = {relative: relative for relative in bundle_files}
    return manifest, bundle_files, targets


def compose_local_release(
    root: Path,
    artifact_dir: Path,
) -> tuple[dict[str, Any], dict[str, bytes], dict[str, str]]:
    root = root.resolve(strict=True)
    resolved_artifacts, triplet_sha = validate_local_candidate(root, artifact_dir)
    return _compose_local_release(root, resolved_artifacts, triplet_sha)


def compose_release(
    root: Path,
    upload_manifest_path: Path,
    artifact_dir: Path,
    *,
    expected_transaction_id: str | None = None,
    expected_manifest_sha256: str | None = None,
    expected_delivery_basis: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, bytes], dict[str, str]]:
    root = root.resolve(strict=True)
    _upload, delivered_triplet = validate_delivery(
        root,
        upload_manifest_path,
        artifact_dir,
        expected_transaction_id=expected_transaction_id,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_delivery_basis=expected_delivery_basis,
    )
    manifest, bundle_files, targets = compose_local_release(root, artifact_dir)
    composed_triplet = {
        extension: str(reference["sha256"])
        for extension, reference in manifest["artifacts"].items()
    }
    if composed_triplet != delivered_triplet:
        raise R009PromotionError(
            "fresh receipt release digest does not match the local candidate"
        )
    return manifest, bundle_files, targets


def _stage_verifier(
    root: Path,
    targets: Mapping[str, str],
):
    def verify(stage: Path, manifest_path: Path, digest: str) -> None:
        overrides = {target: stage / source for target, source in targets.items()}
        verify_release_manifest(
            root,
            manifest_path,
            expected_manifest_sha256=digest,
            path_overrides=overrides,
        )

    return verify


def stage_local_candidate(root: Path, artifact_dir: Path) -> dict[str, Any]:
    """Stage a verified candidate without publishing current or legacy mirrors."""

    root = root.resolve(strict=True)
    manifest, bundle_files, targets = compose_local_release(root, artifact_dir)
    result = AtomicReleasePublisher(root).stage_candidate(
        manifest=manifest,
        bundle_files=bundle_files,
        stage_verifier=_stage_verifier(root, targets),
    )
    verification = verify_release_manifest(
        root,
        root / result["manifest_path"],
        expected_manifest_sha256=result["manifest_sha256"],
    )
    return {
        **result,
        "schema": "step5d.autotune-v3/local-release-candidate-v1",
        "program": PROGRAM,
        "verification": verification,
    }


def promote(
    root: Path,
    upload_manifest_path: Path,
    artifact_dir: Path,
    *,
    expected_transaction_id: str,
    expected_manifest_sha256: str,
    expected_candidate_manifest_sha256: str | None = None,
    expected_delivery_basis: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    root = root.resolve(strict=True)
    manifest, bundle_files, targets = compose_release(
        root,
        upload_manifest_path,
        artifact_dir,
        expected_transaction_id=expected_transaction_id,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_delivery_basis=expected_delivery_basis,
    )

    composed_digest = _sha256_bytes(canonical_bytes(manifest))
    if (
        expected_candidate_manifest_sha256 is not None
        and composed_digest != expected_candidate_manifest_sha256
    ):
        raise R009PromotionError(
            "fresh receipt release digest differs from qualified local candidate"
        )

    result = AtomicReleasePublisher(root).publish(
        manifest=manifest,
        bundle_files=bundle_files,
        compatibility_targets=targets,
        stage_verifier=_stage_verifier(root, targets),
    )
    release = load_current_release(root)
    verification = verify_release_manifest(
        root,
        root / release.manifest_path,
        expected_manifest_sha256=release.manifest_sha256,
    )
    return {
        **result,
        "program": release.program_id,
        "protocol": release.protocol_id,
        "verification": verification,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--compose-only", action="store_true")
    parser.add_argument("--stage-local-candidate", action="store_true")
    parser.add_argument("--candidate-output", type=Path)
    args = parser.parse_args(argv)
    if args.stage_local_candidate:
        if args.manifest is not None or args.compose_only:
            parser.error("local candidate staging cannot include a delivery manifest")
        artifact_dir = (
            args.artifact_dir
            if args.artifact_dir is not None
            else default_release_artifact_dir(args.root)
        )
        candidate = stage_local_candidate(args.root, artifact_dir)
        if args.candidate_output is not None:
            output = args.candidate_output.expanduser().resolve(strict=False)
            evidence_root = (args.root / "runs").resolve()
            if output.is_symlink() or not output.is_relative_to(evidence_root):
                parser.error("--candidate-output must be a safe path under runs/")
            atomic_json(output, candidate)
            candidate = {
                **candidate,
                "candidate_path": output.relative_to(args.root.resolve()).as_posix(),
            }
        print(json.dumps(candidate, sort_keys=True))
        return 0
    if args.candidate_output is not None:
        parser.error("--candidate-output requires --stage-local-candidate")
    if args.artifact_dir is None:
        parser.error("--artifact-dir is required for receipt-bound composition")
    if args.manifest is None:
        parser.error("--manifest is required for receipt-bound composition")
    if args.compose_only:
        manifest, bundle, targets = compose_release(
            args.root, args.manifest, args.artifact_dir
        )
        print(
            json.dumps(
                {
                    "manifest_sha256": _sha256_bytes(canonical_bytes(manifest)),
                    "bundle_files": sorted(bundle),
                    "compatibility_targets": sorted(targets),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    parser.error(
        "direct promotion is disabled; use scripts/step5d-autotune-v3.sh "
        "release-contract-check followed by tp-deliver"
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, R009PromotionError, ValueError) as exc:
        print(f"r012 atomic promotion blocked: {exc}", file=__import__("sys").stderr)
        raise SystemExit(2)
