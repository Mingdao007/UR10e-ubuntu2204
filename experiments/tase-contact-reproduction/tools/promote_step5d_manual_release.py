#!/usr/bin/env python3
"""Atomically publish a fresh-GET manual-hold release without changing r009 current."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

import build_step5d_manual_tp_v1 as builder
from step5d_manual_atomic_release import ManualAtomicReleasePublisher, canonical_bytes
from step5d_autotune_v3.release_identity import load_current_release


ROOT = Path(__file__).resolve().parents[1]
PROGRAM = builder.PROGRAM_NAME
PROTOCOL = builder.PROTOCOL
TARGET_DIR = "/programs/andyl/kunwei/step5"
PACKAGE_DIR = Path("programs/step5/step5d")
EXTENSIONS = (".script", ".txt", ".urp")
POINTER_SCHEMA = "step5d.manual-hold/current-release-pointer-v1"
MANIFEST_SCHEMA = "step5d.manual-hold/release-manifest-v1"
MANUAL_RELEASE_ROOT = "config/step5d/manual/releases"
MANUAL_POINTER = "config/step5d/manual/current.json"
MANUAL_READBACK = Path("config/step5d/manual/controller_readback.json")
MANUAL_CANDIDATE = Path("config/step5d/manual/local_candidate.json")
SOURCE_INPUTS = (
    Path("STEP5D_MANUAL_HOLD.md"),
    Path("config/step5d/manual/launch_profile.json"),
    Path("config/step5d/manual/stage_table.json"),
    Path("config/step5/step5d_autotune_v3_control_contract.json"),
    Path("scripts/step5d-autotune-v3.sh"),
    Path("tools/build_step5d_manual_tp_v1.py"),
    Path("tools/step5d_manual_queue.py"),
    Path("tools/step5d_manual_runtime.py"),
    Path("tools/run_step5d_manual_hold_campaign.py"),
    Path("tools/step5d_manual_bridge.py"),
    Path("tools/build_step5d_manual_bridge_start_context.py"),
    Path("tools/preflight_step5d_manual_bridge.py"),
    Path("tools/run_step5d_manual_bridge.py"),
    Path("tools/run_step5d_manual_bridge_live.py"),
    Path("tools/run_step5d_manual_live_campaign.py"),
    Path("tools/step5d_manual_campaign_plan.py"),
    Path("tools/step5d_manual_profile.py"),
    Path("tools/step5d_manual_status.py"),
    Path("tools/resolve_step5d_bridge_route.py"),
    Path("tools/step5d_manual_atomic_release.py"),
    Path("tools/promote_step5d_manual_release.py"),
    Path("tools/run_step5d_manual_tp_transaction.py"),
    Path("tools/preflight_step5d_autotune_v3.py"),
    Path("tools/run_step5d_autotune_v3_bridge.py"),
    Path("tools/step5d_autotune_contract.py"),
    Path("tools/step5d_autotune_evaluator.py"),
    Path("tools/step5d_autotune_live_driver.py"),
    Path("tools/step5d_autotune_state_machine.py"),
    Path("tools/step5d_autotune_v3/dashboard.py"),
    Path("tools/step5d_autotune_v3/launcher.py"),
    Path("tools/step5d_autotune_v3/preflight_support.py"),
    Path("tools/step5d_autotune_v3/profile.py"),
    Path("tools/step5d_autotune_v3/rtde_client.py"),
    Path("tools/step5d_autotune_v3/runtime_profile.py"),
    Path("tools/step5d_autotune_v3/state.py"),
    Path("tools/ur10e_parallel.py"),
)
_TRANSACTION = re.compile(r"^[0-9a-f]{32}$")


class ManualPromotionError(RuntimeError):
    pass


def _sha256_bytes(encoded: bytes) -> str:
    return hashlib.sha256(encoded).hexdigest()


def _sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ManualPromotionError(f"required regular file is missing: {path}")
    return _sha256_bytes(path.read_bytes())


def _load(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ManualPromotionError(f"required regular JSON file is missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManualPromotionError(f"invalid JSON at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ManualPromotionError(f"JSON object required: {path}")
    return payload


def _pretty(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _artifact_paths(root: Path) -> dict[str, Path]:
    return {
        extension: root / PACKAGE_DIR / f"{PROGRAM}{extension}"
        for extension in EXTENSIONS
    }


def validate_delivery(root: Path, manifest_path: Path) -> tuple[dict[str, Any], dict[str, str]]:
    root = root.resolve(strict=True)
    manifest_path = manifest_path.resolve(strict=True)
    manifest = _load(manifest_path)
    files = _artifact_paths(root)
    local_sha = {extension: _sha256(path) for extension, path in files.items()}
    validation = manifest.get("validation")
    hashes = manifest.get("sha256")
    transaction = manifest.get("upload_transaction_id")
    if not isinstance(validation, Mapping) or not isinstance(hashes, Mapping):
        raise ManualPromotionError("delivery validation or SHA closure is missing")
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
        raise ManualPromotionError("manual delivery identity, target, or freshness differs")
    if not (hashes.get("local") == hashes.get("controller") == hashes.get("readback") == local_sha):
        raise ManualPromotionError("manual local/controller/readback triplet SHA closure differs")
    for extension, key in {
        ".script": "script_sha256",
        ".txt": "txt_sha256",
        ".urp": "urp_sha256",
    }.items():
        if validation.get(key) != local_sha[extension]:
            raise ManualPromotionError(f"manual delivery validation SHA differs: {extension}")
        if _sha256(manifest_path.parent / f"{PROGRAM}{extension}") != local_sha[extension]:
            raise ManualPromotionError(f"manual fresh GET bytes differ: {extension}")
    return manifest, local_sha


def _verify_manual_stage(stage: Path, manifest_path: Path, digest: str) -> None:
    document = _load(manifest_path)
    if document.get("schema") != MANIFEST_SCHEMA:
        raise ManualPromotionError("staged manual manifest schema differs")
    if _sha256_bytes(canonical_bytes(document)) != digest:
        raise ManualPromotionError("staged manual manifest digest differs")
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != set(EXTENSIONS):
        raise ManualPromotionError("staged manual artifact triplet differs")
    paths: dict[str, Path] = {}
    for extension, reference in artifacts.items():
        if not isinstance(reference, Mapping) or set(reference) != {"path", "sha256"}:
            raise ManualPromotionError("staged manual artifact reference differs")
        path = stage / reference["path"]
        if _sha256(path) != reference["sha256"]:
            raise ManualPromotionError(f"staged manual artifact SHA differs: {extension}")
        paths[extension] = path
    builder.validate_triplet(
        paths[".script"].read_text(encoding="utf-8"),
        paths[".txt"].read_text(encoding="utf-8"),
        paths[".urp"].read_bytes(),
        document["identity"]["source_stamp"],
    )


def compose_release(
    root: Path, upload_manifest_path: Path
) -> tuple[dict[str, Any], dict[str, bytes], dict[str, str]]:
    root = root.resolve(strict=True)
    parent = load_current_release(root)
    if (
        parent.program_id != "step5d_strict_rnn_autotune_v3_r009"
        or parent.protocol_id != "v3_full_home_rolling_arm_v1"
    ):
        raise ManualPromotionError("manual release parent is not frozen r009 rolling-v1")
    upload, triplet_sha = validate_delivery(root, upload_manifest_path)
    deploy = PACKAGE_DIR / f"{PROGRAM}.deploy-manifest.json"
    numeric = PACKAGE_DIR / f"{PROGRAM}.numeric-sanity.json"
    source_stamp = str(upload["validation"]["stamp"])
    readback = {
        "schema": "step5d.manual-hold/controller-readback-v1",
        "status": "controller read-back verified",
        "verified": True,
        "program": PROGRAM,
        "protocol": PROTOCOL,
        "parent_r009_commit": builder.PARENT_R009_COMMIT,
        "parent_r009_release_manifest_sha256": parent.manifest_sha256,
        "controller": upload["controller"],
        "controller_target": str(PurePosixPath(TARGET_DIR) / f"{PROGRAM}.urp"),
        "fresh_controller_checked_at": upload["fresh_controller_checked_at"],
        "source_stamp": source_stamp,
        "triplet_sha256": dict(triplet_sha),
        "fresh_readback_manifest_sha256": _sha256(upload_manifest_path),
        "fresh_get": True,
        "safety_boundary": [
            "controller package upload and fresh GET only",
            "no Load or Play",
            "no bridge, ARM, contact, or motion",
        ],
    }
    candidate = {
        "schema": "step5d.manual-hold/local-candidate-v1",
        "program": PROGRAM,
        "protocol": PROTOCOL,
        "parent_r009_commit": builder.PARENT_R009_COMMIT,
        "controller_readback_verified": True,
        "motion_authorized": False,
        "default_request": {
            "force_p_gain": 0.001,
            "force_i_gain": 0.0001,
            "force_damping": 7.0,
            "orientation_ko": 0.4,
            "target_force_n": 12.0,
            "execution_profile_id": "nf100-slew050-a050",
            "execution_profile_integer_id": 633,
            "stage25_success_target_s": 60.0,
        },
    }
    bundle_files: dict[str, bytes] = {}
    for extension, path in _artifact_paths(root).items():
        bundle_files[(PACKAGE_DIR / f"{PROGRAM}{extension}").as_posix()] = path.read_bytes()
    bundle_files[deploy.as_posix()] = (root / deploy).read_bytes()
    bundle_files[numeric.as_posix()] = (root / numeric).read_bytes()
    readback_relative = Path("config/step5d/manual/readbacks") / _sha256(upload_manifest_path) / "manifest.json"
    bundle_files[readback_relative.as_posix()] = upload_manifest_path.read_bytes()
    bundle_files[MANUAL_READBACK.as_posix()] = _pretty(readback)
    bundle_files[MANUAL_CANDIDATE.as_posix()] = _pretty(candidate)
    artifacts = {
        extension: {
            "path": (PACKAGE_DIR / f"{PROGRAM}{extension}").as_posix(),
            "sha256": digest,
        }
        for extension, digest in triplet_sha.items()
    }
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "identity": {
            "program_id": PROGRAM,
            "protocol_id": PROTOCOL,
            "parent_r009_commit": builder.PARENT_R009_COMMIT,
            "parent_r009_release_manifest_sha256": parent.manifest_sha256,
            "source_stamp": source_stamp,
            "execution_profile_id": "nf100-slew050-a050",
            "execution_profile_integer_id": 633,
        },
        "artifacts": artifacts,
        "controller_readback": {
            "path": MANUAL_READBACK.as_posix(),
            "sha256": _sha256_bytes(bundle_files[MANUAL_READBACK.as_posix()]),
            "triplet_sha256": dict(triplet_sha),
            "fresh_get": True,
        },
        "runtime_policy": {
            "queue_schema": "step5d.autotune-v3/manual-queue-v1",
            "logical_batch_size": 1,
            "home_wait_timeout_s": None,
            "home_wait_heartbeat_stale_s": builder.HEARTBEAT_STALE_S,
            "heartbeat_loss_reason": builder.HEARTBEAT_LOSS_REASON,
            "identity_fault_reason": builder.MANUAL_IDENTITY_REASON,
            "completion_state": 77,
            "complete_command": 4,
        },
        "default_request": candidate["default_request"],
        "source_fingerprints": {
            relative.as_posix(): _sha256((root / relative).resolve())
            for relative in SOURCE_INPUTS
        },
        "generated_files": {
            deploy.as_posix(): _sha256(root / deploy),
            numeric.as_posix(): _sha256(root / numeric),
            MANUAL_READBACK.as_posix(): _sha256_bytes(bundle_files[MANUAL_READBACK.as_posix()]),
            MANUAL_CANDIDATE.as_posix(): _sha256_bytes(bundle_files[MANUAL_CANDIDATE.as_posix()]),
        },
        "verification": {
            "canonical_verifier": "manual_script_urp_parent_r009_v1",
            "pointer_switched_last": True,
            "canonical_r009_pointer_untouched": True,
        },
    }
    targets = {
        MANUAL_READBACK.as_posix(): MANUAL_READBACK.as_posix(),
        MANUAL_CANDIDATE.as_posix(): MANUAL_CANDIDATE.as_posix(),
    }
    return manifest, bundle_files, targets


def promote(root: Path, upload_manifest_path: Path) -> dict[str, Any]:
    root = root.resolve(strict=True)
    canonical_before = (root / "config/step5d/current.json").read_bytes()
    manifest, bundle, targets = compose_release(root, upload_manifest_path)
    result = ManualAtomicReleasePublisher(root).publish(
        manifest=manifest,
        bundle_files=bundle,
        compatibility_targets=targets,
        stage_verifier=_verify_manual_stage,
    )
    if (root / "config/step5d/current.json").read_bytes() != canonical_before:
        raise ManualPromotionError("manual promotion changed canonical r009 pointer")
    return {
        **result,
        "program": PROGRAM,
        "protocol": PROTOCOL,
        "controller_readback_verified": True,
        "canonical_r009_pointer_untouched": True,
        "motion_authorized": False,
    }


def load_manual_release(root: Path) -> dict[str, Any]:
    """Independently verify the isolated pointer and every referenced byte."""

    root = root.resolve(strict=True)
    pointer_path = root / MANUAL_POINTER
    pointer = _load(pointer_path)
    if set(pointer) != {"schema", "manifest_path", "manifest_sha256"}:
        raise ManualPromotionError("manual release pointer fields differ")
    digest = pointer["manifest_sha256"]
    if pointer["schema"] != POINTER_SCHEMA or not isinstance(digest, str) or len(digest) != 64:
        raise ManualPromotionError("manual release pointer identity differs")
    expected_path = (Path(MANUAL_RELEASE_ROOT) / digest / "manifest.json").as_posix()
    if pointer["manifest_path"] != expected_path:
        raise ManualPromotionError("manual release pointer is not content-addressed")
    manifest_path = root / expected_path
    manifest = _load(manifest_path)
    if manifest_path.read_bytes() != canonical_bytes(manifest):
        raise ManualPromotionError("manual release manifest is not canonical JSON")
    if _sha256(manifest_path) != digest or manifest.get("schema") != MANIFEST_SCHEMA:
        raise ManualPromotionError("manual release manifest SHA or schema differs")
    identity = manifest.get("identity")
    if not isinstance(identity, Mapping) or any(
        (
            identity.get("program_id") != PROGRAM,
            identity.get("protocol_id") != PROTOCOL,
            identity.get("parent_r009_commit") != builder.PARENT_R009_COMMIT,
        )
    ):
        raise ManualPromotionError("manual release identity differs")
    for role in ("artifacts", "source_fingerprints", "generated_files"):
        references = manifest.get(role)
        if not isinstance(references, Mapping) or not references:
            raise ManualPromotionError(f"manual release {role} is empty")
        for relative, reference in references.items():
            expected = reference.get("sha256") if isinstance(reference, Mapping) else reference
            path_value = reference.get("path") if isinstance(reference, Mapping) else relative
            if not isinstance(path_value, str) or not isinstance(expected, str):
                raise ManualPromotionError(f"manual release {role} reference differs")
            if _sha256(root / path_value) != expected:
                raise ManualPromotionError(f"manual release {role} fingerprint drifted: {path_value}")
    parent_sha = identity.get("parent_r009_release_manifest_sha256")
    parent_path = root / "config/step5d/releases" / str(parent_sha) / "manifest.json"
    parent = _load(parent_path)
    if (
        _sha256(parent_path) != parent_sha
        or parent.get("identity", {}).get("program_id")
        != "step5d_strict_rnn_autotune_v3_r009"
        or parent.get("identity", {}).get("protocol_id")
        != "v3_full_home_rolling_arm_v1"
    ):
        raise ManualPromotionError("manual immutable parent r009 manifest differs")
    if manifest["default_request"]["force_i_gain"] != 0.0001:
        raise ManualPromotionError("manual default I gain is not 1e-4")
    return {
        "ok": True,
        "manifest_path": expected_path,
        "manifest_sha256": digest,
        "program": PROGRAM,
        "protocol": PROTOCOL,
        "force_i_gain": 0.0001,
        "motion_authorized": False,
    }


def rebind_host_sources(root: Path) -> dict[str, Any]:
    """Atomically bind current host code to the unchanged verified TP triplet."""

    root = root.resolve(strict=True)
    pointer = _load(root / MANUAL_POINTER)
    manifest_path = root / str(pointer.get("manifest_path", ""))
    manifest = _load(manifest_path)
    if _sha256(manifest_path) != pointer.get("manifest_sha256"):
        raise ManualPromotionError("manual source release pointer differs")
    identity = manifest.get("identity")
    if not isinstance(identity, Mapping) or any(
        (
            identity.get("program_id") != PROGRAM,
            identity.get("protocol_id") != PROTOCOL,
            identity.get("parent_r009_commit") != builder.PARENT_R009_COMMIT,
        )
    ):
        raise ManualPromotionError("manual source release identity differs")
    rebound = copy.deepcopy(manifest)
    rebound["source_fingerprints"] = {
        relative.as_posix(): _sha256((root / relative).resolve())
        for relative in SOURCE_INPUTS
    }
    verification = rebound.get("verification")
    if not isinstance(verification, dict):
        raise ManualPromotionError("manual release verification block differs")
    verification["host_source_rebound"] = True
    bundle_paths: set[str] = set()
    for role in ("artifacts", "generated_files"):
        references = rebound.get(role)
        if not isinstance(references, Mapping):
            raise ManualPromotionError(f"manual release {role} differs")
        for relative, reference in references.items():
            bundle_paths.add(
                str(reference.get("path"))
                if isinstance(reference, Mapping)
                else str(relative)
            )
    controller_readback = rebound.get("controller_readback")
    if not isinstance(controller_readback, Mapping):
        raise ManualPromotionError("manual controller readback reference differs")
    bundle_paths.add(str(controller_readback.get("path")))
    bundle = {relative: (root / relative).read_bytes() for relative in bundle_paths}

    def verify(stage: Path, path: Path, digest: str) -> None:
        document = _load(path)
        if _sha256_bytes(canonical_bytes(document)) != digest:
            raise ManualPromotionError("rebound manual manifest digest differs")
        for role in ("artifacts", "generated_files", "source_fingerprints"):
            references = document.get(role)
            if not isinstance(references, Mapping) or not references:
                raise ManualPromotionError(f"rebound manual {role} differs")
            for relative, reference in references.items():
                expected = reference.get("sha256") if isinstance(reference, Mapping) else reference
                path_value = reference.get("path") if isinstance(reference, Mapping) else relative
                base = stage if role != "source_fingerprints" else root
                if _sha256(base / str(path_value)) != expected:
                    raise ManualPromotionError(f"rebound manual {role} fingerprint differs")

    canonical_before = (root / "config/step5d/current.json").read_bytes()
    result = ManualAtomicReleasePublisher(root).publish(
        manifest=rebound,
        bundle_files=bundle,
        compatibility_targets={
            MANUAL_READBACK.as_posix(): MANUAL_READBACK.as_posix(),
            MANUAL_CANDIDATE.as_posix(): MANUAL_CANDIDATE.as_posix(),
        },
        stage_verifier=verify,
    )
    if (root / "config/step5d/current.json").read_bytes() != canonical_before:
        raise ManualPromotionError("manual host rebind changed canonical V3 pointer")
    return {**result, "triplet_unchanged": True, "controller_write": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--compose-only", action="store_true")
    parser.add_argument("--verify-current", action="store_true")
    parser.add_argument("--rebind-host-sources", action="store_true")
    args = parser.parse_args(argv)
    if args.rebind_host_sources:
        if args.manifest is not None or args.compose_only or args.verify_current:
            parser.error("--rebind-host-sources cannot be combined with other actions")
        print(json.dumps(rebind_host_sources(args.root), indent=2, sort_keys=True))
        return 0
    if args.verify_current:
        if args.manifest is not None or args.compose_only:
            parser.error("--verify-current cannot be combined with --manifest/--compose-only")
        print(json.dumps(load_manual_release(args.root), indent=2, sort_keys=True))
        return 0
    if args.manifest is None:
        parser.error("--manifest is required unless --verify-current is used")
    if args.compose_only:
        manifest, bundle, targets = compose_release(args.root, args.manifest)
        print(json.dumps({
            "manifest_sha256": _sha256_bytes(canonical_bytes(manifest)),
            "bundle_files": sorted(bundle),
            "compatibility_targets": sorted(targets),
        }, indent=2, sort_keys=True))
        return 0
    print(json.dumps(promote(args.root, args.manifest), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ManualPromotionError, ValueError) as exc:
        print(f"manual release promotion blocked: {exc}", file=__import__("sys").stderr)
        raise SystemExit(2)
