#!/usr/bin/env python3
"""Assemble and cold-validate the immutable, offline R011 release."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from step5d_autotune_v4_r011.contracts import (
    DEFAULT_CONTRACT_PATH,
    DEFAULT_IDENTITY_PATH,
    Contract,
    contract_bytes,
    identity_bytes,
    load_contract,
)
from step5d_autotune_v4_r011.identity import (
    BEHAVIOR_MANIFEST_SCHEMA,
    BehaviorManifest,
    build_behavior_manifest,
    canonical_bytes,
)
from step5d_autotune_v4_r011.ledger import header_record


ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config/step5d"
MANIFEST_PATH = CONFIG_DIR / "autotune_v4_r011.behavior-manifest.json"
CLOSURE_PATH = CONFIG_DIR / "autotune_v4_r011_offline_closure.json"
EMPTY_LEDGER_PATH = CONFIG_DIR / "autotune_v4_r011_empty_ledger.jsonl"
IDENTITY_ALIAS_PATH = CONFIG_DIR / "r011_release_identity.json"


class R011BuilderError(RuntimeError):
    """R011 assembly cannot prove a complete offline closure."""


def assemble_release() -> tuple[BehaviorManifest, Contract, dict[Path, bytes]]:
    manifest = build_behavior_manifest()
    triplet = dict(manifest.raw["controller_triplet_sha256"])
    from step5d_autotune_v4_r011.contracts import build_contract

    bundle = build_contract(behavior_manifest=manifest, controller_triplet_sha256=triplet)
    binding = {
        "schema": "step5d.autotune-v4/r011-observation-release-binding-v1",
        "release_identity_sha256": bundle.release_identity_sha256,
        "campaign_fingerprint": bundle.campaign_fingerprint,
        "empty_ledger_only": True,
        "historical_observations_imported": False,
        "wave_is_bo_variable": False,
        "live_authority": False,
    }
    targets = {
        DEFAULT_CONTRACT_PATH: contract_bytes(bundle.raw),
        MANIFEST_PATH: canonical_bytes(manifest.as_dict()) + b"\n",
        CLOSURE_PATH: canonical_bytes(manifest.source_closure.as_dict()) + b"\n",
        DEFAULT_IDENTITY_PATH: identity_bytes(bundle.release_identity),
        IDENTITY_ALIAS_PATH: identity_bytes(bundle.release_identity),
        EMPTY_LEDGER_PATH: canonical_bytes(header_record(bundle.release_identity)) + b"\n",
        CONFIG_DIR / "autotune_v4_r011.observation-binding.json": canonical_bytes(binding) + b"\n",
    }
    return manifest, bundle, targets


def _atomic_batch(targets: Mapping[Path, bytes]) -> None:
    staged: dict[Path, Path] = {}
    previous: dict[Path, bytes | None] = {}
    replaced: list[Path] = []
    try:
        for target, encoded in targets.items():
            target = Path(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink() or target.parent.is_symlink():
                raise R011BuilderError(f"unsafe R011 target: {target}")
            previous[target] = target.read_bytes() if target.exists() and target.is_file() else None
            descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
            temporary_path = Path(temporary)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            staged[target] = temporary_path
        for target in targets:
            os.replace(staged[Path(target)], Path(target))
            replaced.append(Path(target))
        for directory in {Path(target).parent for target in targets}:
            descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except BaseException as exc:
        rollback_errors: list[str] = []
        for target in reversed(replaced):
            try:
                old = previous[target]
                if old is None:
                    target.unlink(missing_ok=True)
                else:
                    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.rollback.", dir=target.parent)
                    temporary_path = Path(temporary)
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(old)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary_path, target)
            except OSError as rollback_exc:
                rollback_errors.append(f"{target}: {rollback_exc}")
        detail = f"; rollback failures: {rollback_errors}" if rollback_errors else ""
        raise R011BuilderError(f"R011 failure-atomic release persist failed{detail}") from exc
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)


def build_release(*, persist: bool = False) -> dict[str, Any]:
    manifest, bundle, targets = assemble_release()
    if persist:
        _atomic_batch(targets)
    else:
        for target, encoded in targets.items():
            if not target.is_file() or target.is_symlink() or target.read_bytes() != encoded:
                raise R011BuilderError(f"R011 cold check differs: {target}")
    cold = load_contract(DEFAULT_CONTRACT_PATH, identity_path=DEFAULT_IDENTITY_PATH)
    if cold.sha256 != bundle.sha256 or cold.release_identity_sha256 != bundle.release_identity_sha256:
        raise R011BuilderError("R011 cold-loaded contract/identity differs from builder")
    return {
        "ok": True,
        "schema": BEHAVIOR_MANIFEST_SCHEMA,
        "program": manifest.raw["program"],
        "campaign_fingerprint": manifest.campaign_fingerprint,
        "behavior_manifest_sha256": manifest.behavior_manifest_sha256,
        "source_closure_sha256": manifest.source_closure.sha256,
        "contract_sha256": bundle.sha256,
        "release_identity_sha256": bundle.release_identity_sha256,
        "controller_triplet_sha256": dict(manifest.raw["controller_triplet_sha256"]),
        "controller_triplet_provenance": manifest.as_dict()["controller_triplet_provenance"],
        "runtime_protocol": 609009,
        "persisted": persist,
        "controller_upload": False,
        "controller_readback": False,
        "current_pointer_switch": False,
        "live_evidence": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--persist", action="store_true", help="explicitly persist the offline release set")
    args = parser.parse_args()
    print(json.dumps(build_release(persist=args.persist), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
