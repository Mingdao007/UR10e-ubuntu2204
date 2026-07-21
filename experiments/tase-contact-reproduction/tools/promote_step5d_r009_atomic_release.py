#!/usr/bin/env python3
"""Promote one fresh r009 GET into an immutable rolling-v1 release.

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
    CONTROL_PROFILE_ID,
    RELEASE_MANIFEST_SCHEMA,
    RELEASE_STAGE_ID,
    ROLLING_EXECUTION_PROFILE_ID,
    ROLLING_EXECUTION_PROFILE_INTEGER_ID,
    ROLLING_NORMAL_MAX_RATE_RAD_S,
    ROLLING_PROTOCOL,
    load_current_release,
)
from step5d_autotune_v3.release_verifier import verify_release_manifest
from step5d_autotune_v3.profile import contract_sha256


ROOT = Path(__file__).resolve().parents[1]
PROGRAM = "step5d_strict_rnn_autotune_v3_r009"
HISTORICAL_PROGRAM = "step5d_strict_rnn_autotune_v3_r008"
TARGET_DIR = "/programs/andyl/kunwei/step5"
PACKAGE_DIR = Path("programs/step5/step5d")
EXTENSIONS = (".script", ".txt", ".urp")
READBACK = Path("config/step5d_autotune_controller_readback_v3.json")
RAW_READBACK = Path(
    "config/step5d/manifests/step5d_strict_rnn_autotune_v3_r009/"
    "controller_readback.json"
)
LOCAL_CANDIDATE = Path(
    "config/step5d/manifests/step5d_strict_rnn_autotune_v3_r009/"
    "local_candidate.json"
)
STATIC_PROJECTIONS = (
    Path("config/current_stage.json"),
    Path("config/step5_stage_table.json"),
    Path("config/tase_protocol_table.json"),
    Path("config/step5d/v3_active_surface.json"),
    Path("config/step5/step5d_autotune_v3_control_contract.json"),
    Path("config/step5/step5d_autotune_v3_launch_profile.json"),
)
SOURCE_INPUTS = (
    Path("tools/build_step5d_autotune_tp_v3.py"),
    Path("tools/promote_step5d_r009_atomic_release.py"),
    Path("tools/run_step5d_autotune_campaign.py"),
    Path("tools/run_step5d_autotune_v3_bridge.py"),
    Path("tools/run_step5d_autotune_v3_live.py"),
    Path("tools/step5d_autotune_batch_plan.py"),
    Path("tools/step5d_autotune_r008_policy.py"),
    Path("tools/step5d_autotune_runtime_lifecycle.py"),
    Path("tools/step5d_autotune_v3/atomic_release.py"),
    Path("tools/step5d_autotune_v3/release_identity.py"),
    Path("tools/step5d_autotune_v3/release_verifier.py"),
    Path("tools/step5d_autotune_v3/readiness.py"),
    Path("tools/step5d_autotune_v3/runtime_profile.py"),
)
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


def _project(value: Any) -> Any:
    """Restore historical labels, then project only the active TP program."""

    if isinstance(value, str):
        historical = value.replace("r009", "r008")
        return historical.replace(HISTORICAL_PROGRAM, PROGRAM)
    if isinstance(value, list):
        return [_project(item) for item in value]
    if isinstance(value, dict):
        return {_project(key): _project(item) for key, item in value.items()}
    return value


def _render_contract(
    root: Path,
    source: Mapping[str, Any],
    *,
    triplet_sha256: Mapping[str, str],
    deploy_manifest_sha256: str,
    numeric_sanity_sha256: str,
    readback_sha256: str,
) -> dict[str, Any]:
    base = _project(source)
    source_sha256 = {
        relative: _sha256(root / relative)
        for relative in base["source_sha256"]
    }
    return {
        **base,
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
) -> dict[str, Any]:
    base = _project(source)
    return {
        **base,
        "release_stage_id": RELEASE_STAGE_ID,
        "control_profile_id": CONTROL_PROFILE_ID,
        "tp_program_id": PROGRAM,
        "control_contract_sha256": contract_sha256,
    }


def _artifact_paths(root: Path) -> dict[str, Path]:
    return {
        extension: root / PACKAGE_DIR / f"{PROGRAM}{extension}"
        for extension in EXTENSIONS
    }


def validate_delivery(
    root: Path,
    manifest_path: Path,
) -> tuple[dict[str, Any], dict[str, str]]:
    root = root.resolve(strict=True)
    manifest_path = manifest_path.resolve(strict=True)
    manifest = _load(manifest_path)
    files = _artifact_paths(root)
    local_sha = {extension: _sha256(path) for extension, path in files.items()}
    validation = manifest.get("validation")
    hashes = manifest.get("sha256")
    transaction = manifest.get("upload_transaction_id")
    if not isinstance(validation, Mapping) or not isinstance(hashes, Mapping):
        raise R009PromotionError("delivery validation or SHA closure is missing")
    if any(
        (
            manifest.get("status") != "controller read-back verified",
            manifest.get("delivery_mode") != "full_upload_readback",
            manifest.get("readback_source") != "fresh_controller_get",
            manifest.get("fresh_controller_sha_verified") is not True,
            manifest.get("target_dir") != TARGET_DIR,
            validation.get("program") != PROGRAM,
            validation.get("target_dir") != TARGET_DIR,
            validation.get("script_node_path")
            != str(PurePosixPath(TARGET_DIR) / f"{PROGRAM}.script"),
            not isinstance(transaction, str),
            _TRANSACTION.fullmatch(transaction or "") is None,
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
    upload_manifest: Mapping[str, Any],
    *,
    upload_manifest_sha256: str,
    triplet_sha256: Mapping[str, str],
    tp_fingerprint: str,
) -> dict[str, Any]:
    return {
        "schema": "step5d.autotune.controller-readback/v3",
        "status": "controller read-back verified",
        "verified": True,
        "program": PROGRAM,
        "control_profile_id": CONTROL_PROFILE_ID,
        "controller": upload_manifest["controller"],
        "controller_target": str(PurePosixPath(TARGET_DIR) / f"{PROGRAM}.urp"),
        "fresh_controller_checked_at": upload_manifest["fresh_controller_checked_at"],
        "source_stamp": upload_manifest["validation"]["stamp"],
        "triplet_sha256": dict(triplet_sha256),
        "tp_fingerprint": tp_fingerprint,
        "fresh_readback_manifest_sha256": upload_manifest_sha256,
        "fresh_readback_source": RAW_READBACK.as_posix(),
        "fresh_get": True,
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
        "bridge_context_allowed": True,
        "motion_authorized": False,
        "triplet_sha256": dict(triplet_sha256),
        "deploy_manifest_sha256": deploy_manifest_sha256,
        "numeric_sanity_sha256": numeric_sanity_sha256,
        "controller_readback_sha256": readback_sha256,
        "next_legal_action": "build_fresh_bridge_start_context_no_arm",
    }


def compose_release(
    root: Path,
    upload_manifest_path: Path,
) -> tuple[dict[str, Any], dict[str, bytes], dict[str, str]]:
    root = root.resolve(strict=True)
    upload, triplet_sha = validate_delivery(root, upload_manifest_path)
    raw_upload = upload_manifest_path.read_bytes()
    raw_upload_sha = _sha256_bytes(raw_upload)
    deploy = PACKAGE_DIR / f"{PROGRAM}.deploy-manifest.json"
    numeric = PACKAGE_DIR / f"{PROGRAM}.numeric-sanity.json"
    deploy_sha = _sha256(root / deploy)
    numeric_sha = _sha256(root / numeric)
    canonical_readback = _pretty(
        _canonical_readback(
            upload,
            upload_manifest_sha256=raw_upload_sha,
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
    for extension, path in _artifact_paths(root).items():
        bundle_files[(PACKAGE_DIR / f"{PROGRAM}{extension}").as_posix()] = path.read_bytes()
    bundle_files[deploy.as_posix()] = (root / deploy).read_bytes()
    bundle_files[numeric.as_posix()] = (root / numeric).read_bytes()
    bundle_files[RAW_READBACK.as_posix()] = raw_upload
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
    bundle_files[contract_relative.as_posix()] = contract
    bundle_files[launch_relative.as_posix()] = _pretty(
        _render_launch_profile(
            _load(root / launch_relative),
            contract_sha256=contract_sha256(contract_document),
        )
    )
    for relative in STATIC_PROJECTIONS:
        if relative not in {contract_relative, launch_relative}:
            bundle_files[relative.as_posix()] = _pretty(
                _project(_load(root / relative))
            )

    generated_files = {
        relative: _sha256_bytes(bundle_files[relative])
        for relative in (
            deploy.as_posix(),
            numeric.as_posix(),
            RAW_READBACK.as_posix(),
            LOCAL_CANDIDATE.as_posix(),
        )
    }
    compatibility_mirrors = {
        relative.as_posix(): _sha256_bytes(bundle_files[relative.as_posix()])
        for relative in STATIC_PROJECTIONS
    }
    compatibility_mirrors[READBACK.as_posix()] = readback_sha
    source_fingerprints = {
        relative.as_posix(): _sha256(root / relative)
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
        "controller_readback": {
            "path": READBACK.as_posix(),
            "sha256": readback_sha,
            "triplet_sha256": dict(triplet_sha),
            "fresh_get": True,
        },
        "runtime_policy": {
            "candidate_plan_schema": "step5d_autotune_rolling_batch_plan_v1",
            "plan_lifecycle": ["OPEN_READY", "OPEN_EMPTY", "CLOSED_COMPLETE"],
            "state_78_watchdog_s": 30.0,
            "host_plan_wait_budget_max_s": 25.0,
            "completion_state": 77,
            "complete_command": 4,
            "terminal_scope": "bridge_start_ready_no_arm",
        },
        "optimizer_policy": {
            "control_grouping_uid": "ControlCandidateUid",
            "durability_uid": "OccurrenceUid",
            "transport_uid": "TransportCandidateUid",
            "baseline_repeat_policy": "same_control_distinct_occurrence_and_transport",
            "initialization_batch_size": 5,
            "batch_b_requires": [
                "batch_a_complete",
                "sealed_bundle",
                "cold_read",
                "gp_update",
            ],
        },
        "source_fingerprints": source_fingerprints,
        "generated_files": generated_files,
        "compatibility_mirrors": compatibility_mirrors,
        "verification": {
            "canonical_verifier": "independent_script_urp_v1",
            "staged_bytes_required": True,
            "numeric_sanity_is_expected_truth": False,
            "pointer_switched_last": True,
        },
    }
    targets = {relative: relative for relative in bundle_files}
    return manifest, bundle_files, targets


def promote(root: Path, upload_manifest_path: Path) -> dict[str, Any]:
    root = root.resolve(strict=True)
    manifest, bundle_files, targets = compose_release(root, upload_manifest_path)

    def verify_stage(stage: Path, manifest_path: Path, digest: str) -> None:
        overrides = {
            target: stage / source
            for target, source in targets.items()
        }
        verify_release_manifest(
            root,
            manifest_path,
            expected_manifest_sha256=digest,
            path_overrides=overrides,
        )

    result = AtomicReleasePublisher(root).publish(
        manifest=manifest,
        bundle_files=bundle_files,
        compatibility_targets=targets,
        stage_verifier=verify_stage,
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
        "bridge_start_ready": True,
        "motion_arm_ready": False,
        "verification": verification,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--compose-only", action="store_true")
    args = parser.parse_args(argv)
    if args.compose_only:
        manifest, bundle, targets = compose_release(args.root, args.manifest)
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
    print(json.dumps(promote(args.root, args.manifest), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, R009PromotionError, ValueError) as exc:
        print(f"r009 atomic promotion blocked: {exc}", file=__import__("sys").stderr)
        raise SystemExit(2)
