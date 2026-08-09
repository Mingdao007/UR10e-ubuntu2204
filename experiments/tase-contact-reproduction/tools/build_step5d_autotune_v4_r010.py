#!/usr/bin/env python3
"""Build and cold-validate the complete offline R010 release closure."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

# Allow the project builder to run directly from the experiment checkout.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_RUNTIME_SOURCE = _REPOSITORY_ROOT / "src/ur10e_experiment_runtime"
if str(_RUNTIME_SOURCE) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_SOURCE))

from step5d_autotune_v4_r010.behavior import R010_PROGRAM
from step5d_autotune_v4_r010.contracts import (
    Contract,
    build_contract,
    contract_bytes,
    identity_bytes,
    load_contract,
)
from step5d_autotune_v4_r010.identity import (
    BehaviorManifest,
    build_behavior_manifest,
    canonical_bytes,
    sha256_bytes,
)
from step5d_autotune_v4_r010.ledger import header_record
from step5d_autotune_v4_r010.tp import R010_STAMP, build_triplet, validate_triplet


ROOT = Path(__file__).resolve().parents[1]
PROGRAM_DIR = ROOT / "programs/step5/step5d"
CONFIG_DIR = ROOT / "config/step5d"
CONTRACT_PATH = CONFIG_DIR / "autotune_v4_r010.json"
MANIFEST_PATH = CONFIG_DIR / "autotune_v4_r010.behavior-manifest.json"
CLOSURE_PATH = CONFIG_DIR / "autotune_v4_r010_offline_closure.json"
IDENTITY_PATH = CONFIG_DIR / "autotune_v4_r010.release-identity.json"
IDENTITY_ALIAS_PATH = CONFIG_DIR / "r010_release_identity.json"
EMPTY_LEDGER_PATH = CONFIG_DIR / "autotune_v4_r010_empty_ledger.jsonl"
OBSERVATION_BINDING_PATH = CONFIG_DIR / "autotune_v4_r010.observation-binding.json"
ARTIFACT_SUFFIXES = (
    ".script",
    ".txt",
    ".urp",
    ".numeric-sanity.json",
    ".deploy-manifest.json",
)


class R010BuilderError(RuntimeError):
    """The R010 offline builder cannot prove or persist a complete closure."""


def _triplet_hashes(paths: Mapping[str, Path]) -> dict[str, str]:
    return {
        suffix.lstrip("."): sha256_bytes(Path(paths[suffix]).read_bytes())
        for suffix in (".script", ".txt", ".urp")
    }


def _atomic_batch(target_bytes: Mapping[Path, bytes]) -> None:
    """Persist a bounded release set and restore exact old bytes on failure."""

    staged: dict[Path, Path] = {}
    previous: dict[Path, bytes | None] = {}
    replaced: list[Path] = []
    try:
        for target, encoded in target_bytes.items():
            target = Path(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink() or target.parent.is_symlink():
                raise R010BuilderError(f"unsafe R010 target: {target}")
            previous[target] = target.read_bytes() if target.exists() else None
            descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
            temporary_path = Path(temporary)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            staged[target] = temporary_path
        for target in target_bytes:
            os.replace(staged[Path(target)], Path(target))
            replaced.append(Path(target))
        for directory in {Path(target).parent for target in target_bytes}:
            descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except BaseException as exc:
        failures: list[str] = []
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
                failures.append(f"{target}: {rollback_exc}")
        detail = f"; rollback failures: {'; '.join(failures)}" if failures else ""
        raise R010BuilderError(f"R010 atomic release persist failed{detail}") from exc
    finally:
        for path in staged.values():
            path.unlink(missing_ok=True)


def _release_bytes(
    manifest: BehaviorManifest,
    bundle: Contract,
    generated: Mapping[str, Path],
) -> dict[Path, bytes]:
    identity = bundle.release_identity
    binding = {
        "schema": "step5d.autotune-v4/r010-observation-release-binding-v1",
        "release_identity_sha256": identity.release_identity_sha256,
        "campaign_fingerprint": identity.campaign_fingerprint,
        "empty_ledger_only": True,
        "historical_observations_imported": False,
    }
    targets: dict[Path, bytes] = {
        CONTRACT_PATH: contract_bytes(bundle.raw),
        MANIFEST_PATH: canonical_bytes(manifest.as_dict()) + b"\n",
        CLOSURE_PATH: canonical_bytes(manifest.source_closure.as_dict()) + b"\n",
        IDENTITY_PATH: identity_bytes(identity),
        IDENTITY_ALIAS_PATH: identity_bytes(identity),
        EMPTY_LEDGER_PATH: canonical_bytes(header_record(identity)) + b"\n",
        OBSERVATION_BINDING_PATH: canonical_bytes(binding) + b"\n",
    }
    for suffix in ARTIFACT_SUFFIXES:
        targets[PROGRAM_DIR / f"{R010_PROGRAM}{suffix}"] = Path(generated[suffix]).read_bytes()
    return targets


def build_release(*, persist: bool = True, stamp: str = R010_STAMP) -> dict[str, Any]:
    manifest = build_behavior_manifest()
    with tempfile.TemporaryDirectory(prefix="step5d-r010-offline-") as temporary:
        generated = build_triplet(Path(temporary), behavior_manifest=manifest, stamp=stamp)
        triplet = _triplet_hashes(generated)
        bundle = build_contract(
            behavior_manifest=manifest,
            controller_triplet_sha256=triplet,
        )
        release_bytes = _release_bytes(manifest, bundle, generated)
        if persist:
            _atomic_batch(release_bytes)
        else:
            for target, encoded in release_bytes.items():
                if not target.is_file() or target.is_symlink() or target.read_bytes() != encoded:
                    raise R010BuilderError(f"R010 cold check differs: {target}")

    cold = load_contract(CONTRACT_PATH, identity_path=IDENTITY_PATH)
    if cold.sha256 != bundle.sha256 or cold.release_identity_sha256 != bundle.release_identity_sha256:
        raise R010BuilderError("R010 cold-loaded contract/identity differs from builder")
    script = (PROGRAM_DIR / f"{R010_PROGRAM}.script").read_text(encoding="utf-8")
    txt = (PROGRAM_DIR / f"{R010_PROGRAM}.txt").read_text(encoding="utf-8")
    urp = (PROGRAM_DIR / f"{R010_PROGRAM}.urp").read_bytes()
    validate_triplet(script, txt, urp, stamp, manifest)
    return {
        "ok": True,
        "program": R010_PROGRAM,
        "campaign_fingerprint": manifest.campaign_fingerprint,
        "behavior_manifest_sha256": manifest.behavior_manifest_sha256,
        "source_closure_sha256": manifest.source_closure.sha256,
        "contract_sha256": bundle.sha256,
        "release_identity_sha256": bundle.release_identity_sha256,
        "controller_triplet_sha256": triplet,
        "runtime_protocol": 609009,
        "empty_ledger": str(EMPTY_LEDGER_PATH),
        "persisted": persist,
        "controller_upload": False,
        "controller_readback": False,
        "current_pointer_switch": False,
        "live_evidence": False,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="regenerate in memory and compare cold bytes")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print(json.dumps(build_release(persist=not args.check), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
