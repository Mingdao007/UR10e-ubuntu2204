#!/usr/bin/env python3
"""Promote an exact fresh r008 delivery manifest into the V3 current binding.

This owner is filesystem-only. It never connects to a controller, loads or
starts a program, opens a bridge, sends ARM, or causes robot motion.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Any, Mapping

import rebuild_step5d_autotune_v3_pre_live_evidence as rebuild


ROOT = Path(__file__).resolve().parents[1]
PROGRAM = "step5d_strict_rnn_autotune_v3_r008"
RELEASE = "step5d_strict_rnn_autotune_v3"
TARGET_DIR = "/programs/andyl/kunwei/step5"
EXTENSIONS = (".script", ".txt", ".urp")
READBACK = Path("config/step5d_autotune_controller_readback_v3.json")
CONTRACT = Path("config/step5/step5d_autotune_v3_control_contract.json")
CURRENT = Path("config/step5d/current.json")
ACTIVE = Path("config/step5d/v3_active_surface.json")
CANDIDATE = Path(
    "config/step5d/manifests/step5d_strict_rnn_autotune_v3_r008/local_candidate.json"
)
PACKAGE_DIR = Path("programs/step5/step5d")
IMMUTABLE_DIR = Path(
    "config/step5d/manifests/step5d_strict_rnn_autotune_v3"
)
_TRANSACTION = re.compile(r"^[0-9a-f]{32}$")


class PromotionError(RuntimeError):
    pass


def _load(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PromotionError(f"required regular file is missing: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise PromotionError(f"JSON object required: {path}")
    return payload


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _encoded(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_delivery(root: Path, manifest_path: Path) -> tuple[dict[str, Any], dict[str, str]]:
    root = root.resolve(strict=True)
    manifest_path = manifest_path.resolve(strict=True)
    manifest = _load(manifest_path)
    validation = manifest.get("validation") or {}
    hashes = manifest.get("sha256") or {}
    local_sha = {
        extension: _sha256(root / PACKAGE_DIR / f"{PROGRAM}{extension}")
        for extension in EXTENSIONS
    }
    expected_target = str(PurePosixPath(TARGET_DIR) / f"{PROGRAM}.urp")
    if (
        manifest.get("status") != "controller read-back verified"
        or manifest.get("delivery_mode") != "full_upload_readback"
        or manifest.get("readback_source") != "fresh_controller_get"
        or manifest.get("fresh_controller_sha_verified") is not True
        or manifest.get("target_dir") != TARGET_DIR
        or validation.get("program") != PROGRAM
        or validation.get("target_dir") != TARGET_DIR
        or validation.get("script_node_path")
        != str(PurePosixPath(TARGET_DIR) / f"{PROGRAM}.script")
        or not str(expected_target).endswith(f"/{PROGRAM}.urp")
    ):
        raise PromotionError("delivery manifest identity or freshness differs")
    transaction_id = manifest.get("upload_transaction_id")
    if not isinstance(transaction_id, str) or _TRANSACTION.fullmatch(transaction_id) is None:
        raise PromotionError("delivery manifest lacks the coordinator transaction identity")
    if not (
        hashes.get("local") == hashes.get("controller") == hashes.get("readback") == local_sha
    ):
        raise PromotionError("local/controller/readback triplet SHA closure differs")
    validation_keys = {
        ".script": "script_sha256",
        ".txt": "txt_sha256",
        ".urp": "urp_sha256",
    }
    if any(validation.get(key) != local_sha[extension] for extension, key in validation_keys.items()):
        raise PromotionError("delivery validation digest differs from the local triplet")
    for extension in EXTENSIONS:
        fetched = manifest_path.parent / f"{PROGRAM}{extension}"
        if fetched.is_symlink() or not fetched.is_file() or _sha256(fetched) != local_sha[extension]:
            raise PromotionError(f"fresh fetched-back artifact differs: {extension}")
    return manifest, local_sha


def _immutable_name(manifest: Mapping[str, Any]) -> str:
    checked = str(manifest.get("fresh_controller_checked_at") or "")
    compact = re.sub(r"[^0-9]", "", checked)[:14]
    if len(compact) != 14:
        raise PromotionError("fresh controller timestamp is missing or invalid")
    return f"controller_readback_{compact}HKT.json"


def promote(root: Path, manifest_path: Path) -> dict[str, Any]:
    root = root.resolve(strict=True)
    manifest, local_sha = validate_delivery(root, manifest_path)
    immutable_relative = IMMUTABLE_DIR / _immutable_name(manifest)
    immutable_path = root / immutable_relative
    immutable_bytes = manifest_path.read_bytes()
    if immutable_path.exists() and immutable_path.read_bytes() != immutable_bytes:
        raise PromotionError("immutable delivery-manifest destination already differs")
    deploy_manifest = root / PACKAGE_DIR / f"{PROGRAM}.deploy-manifest.json"
    numeric_sanity = root / PACKAGE_DIR / f"{PROGRAM}.numeric-sanity.json"
    fingerprint = _sha256(deploy_manifest)
    immutable_sha = hashlib.sha256(immutable_bytes).hexdigest()
    canonical = {
        "schema": "step5d.autotune.controller-readback/v3",
        "status": "controller read-back verified",
        "verified": True,
        "program": PROGRAM,
        "control_profile_id": "step5d_strict_rnn_autotune_v1",
        "controller": manifest["controller"],
        "controller_target": str(PurePosixPath(TARGET_DIR) / f"{PROGRAM}.urp"),
        "fresh_controller_checked_at": manifest["fresh_controller_checked_at"],
        "source_stamp": manifest["validation"]["stamp"],
        "triplet_sha256": local_sha,
        "sha256": copy.deepcopy(manifest["sha256"]),
        "tp_fingerprint": fingerprint,
        "fresh_readback_manifest_sha256": immutable_sha,
        "fresh_readback_source": str(immutable_relative),
        "safety_boundary": [
            "controller package upload and fresh read-back only",
            "r008 is eligible for current promotion only after this exact read-back",
            "no Load, Play, bridge, ARM, contact, or motion",
        ],
    }
    canonical_bytes = _encoded(canonical)
    canonical_sha = hashlib.sha256(canonical_bytes).hexdigest()

    contract = _load(root / CONTRACT)
    contract["candidate_tp_artifact_sha256"] = local_sha
    contract["candidate_tp_identity"].update(
        {
            "program": PROGRAM,
            "mode": "controller_readback_verified_promoted_current",
            "deploy_manifest_sha256": _sha256(deploy_manifest),
            "numeric_sanity_sha256": _sha256(numeric_sanity),
        }
    )
    contract["promotion_status"] = "controller_readback_verified"
    contract["tp_artifact_sha256"] = local_sha
    contract["deployment_tp_identity"].update(
        {
            "program": PROGRAM,
            "readback_manifest": str(READBACK),
            "readback_manifest_sha256": canonical_sha,
            "tp_fingerprint": fingerprint,
        }
    )
    current = _load(root / CURRENT)
    current.update(
        {
            "tp_program_id": PROGRAM,
            "tp_program_disposition": "controller_readback_verified",
            "host_runtime_disposition": "verified_r008_full_home_rolling_production_chain_offline",
            "local_candidate_tp_program_id": PROGRAM,
            "local_candidate_tp_disposition": "controller_readback_verified_promoted_current",
            "state": "bridge_start_ready_no_arm",
            "protocol": "v3_full_home_rolling_arm_v1",
            "local_candidate_manifest": str(CANDIDATE),
        }
    )
    current["deployment"].update(
        {"status": "controller_readback_verified", "verified_for_selected_triplet": True}
    )
    current["execution"].update(
        {"bridge_start_ready": True, "bridge_process_ready": False, "motion_arm_ready": False, "campaign_ready": False, "blockers": []}
    )
    current["claims"].update(
        {
            "new_version_complete": True,
            "r008_offline_release_complete": True,
            "r008_controller_readback_verified": True,
            "controller_verified": True,
            "live_ready": False,
            "live_accepted": False,
        }
    )
    candidate = _load(root / CANDIDATE)
    candidate.update(
        {
            "disposition": "controller_readback_verified_promoted_current",
            "controller_uploaded": True,
            "controller_readback_verified": True,
            "bridge_context_allowed": True,
            "motion_authorized": False,
            "controller_readback": {
                "path": str(READBACK),
                "fresh_manifest": str(immutable_relative),
                "fresh_checked_at": manifest["fresh_controller_checked_at"],
            },
            "next_legal_action": "build_fresh_bridge_start_context_no_arm",
        }
    )
    active = _load(root / ACTIVE)
    active.update(
        {
            "tp_program_id": PROGRAM,
            "tp_program_disposition": "controller_readback_verified",
            "local_candidate_tp_program_id": PROGRAM,
            "local_candidate_tp_disposition": "controller_readback_verified_promoted_current",
            "bridge_start_ready": True,
            "blockers": [],
        }
    )
    active["deployment_paths"] = [
        str(PACKAGE_DIR / f"{PROGRAM}{extension}") for extension in EXTENSIONS
    ] + [str(READBACK)]

    writes = {
        immutable_path: immutable_bytes,
        root / READBACK: canonical_bytes,
        root / CONTRACT: _encoded(contract),
        root / CURRENT: _encoded(current),
        root / CANDIDATE: _encoded(candidate),
        root / ACTIVE: _encoded(active),
    }
    snapshots = {path: path.read_bytes() if path.exists() else None for path in writes}
    try:
        for path, data in writes.items():
            _atomic(path, data)
        rc = rebuild.main(
            ["--root", str(root), "--observed-at", manifest["fresh_controller_checked_at"]]
        )
        if rc != 0:
            raise PromotionError("pre-live evidence rebuild failed")
    except Exception:
        for path, previous in snapshots.items():
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                _atomic(path, previous)
        raise
    return {
        "program": PROGRAM,
        "controller_readback_verified": True,
        "triplet_sha256": local_sha,
        "fresh_manifest": str(immutable_relative),
        "bridge_start_ready": True,
        "motion_arm_ready": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = promote(args.root, args.manifest)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PromotionError, OSError, json.JSONDecodeError) as exc:
        print(f"r006 promotion blocked: {exc}", file=os.sys.stderr)
        raise SystemExit(2)
