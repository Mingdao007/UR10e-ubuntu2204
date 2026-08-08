#!/usr/bin/env python3
"""Build a content-addressed local r006 source/package closure."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Iterable

from step5d_autotune_v4_r006.contracts import (
    ROOT,
    canonical_bytes,
    load_contract,
    sha256_bytes,
    sha256_file,
)


OUTPUT = ROOT / "config/step5d/autotune_v4_r006_offline_closure.json"
PROGRAM = "step5d_strict_rnn_autotune_v4_r006"


def source_paths() -> tuple[Path, ...]:
    manifest = json.loads((ROOT / "config/step5d/autotune_v4_r006_runtime_manifest.json").read_text(encoding="utf-8"))
    paths = [ROOT / relative for relative in manifest["source_closure"]]
    paths.extend(
        ROOT / "programs/step5/step5d" / f"{PROGRAM}{suffix}"
        for suffix in (".script", ".txt", ".urp", ".numeric-sanity.json", ".deploy-manifest.json")
    )
    paths.extend(
        ROOT / relative
        for relative in (
            "config/step5d/lineage_selector_v4_r006.json",
            "config/step5d/autotune_v4_r006_stage_metadata.json",
            "STEP5D_FLOW.md",
            "config/step5_stage_table.json",
        )
    )
    unique = {path.relative_to(ROOT).as_posix(): path for path in paths}
    return tuple(unique[key] for key in sorted(unique))


def artifact_paths() -> tuple[Path, ...]:
    return tuple(
        ROOT / "programs/step5/step5d" / f"{PROGRAM}{suffix}"
        for suffix in (".script", ".txt", ".urp", ".numeric-sanity.json", ".deploy-manifest.json")
    )


def build_document() -> dict[str, object]:
    # Rebuilding the closure is the one controlled bootstrap operation: the
    # previous closure may be stale after a source edit.  The resulting
    # content-addressed basis is still checked by normal load_contract().
    contract = load_contract(verify_source_closure=False)
    paths = source_paths()
    source_hashes = {path.relative_to(ROOT).as_posix(): sha256_file(path) for path in paths}
    generated = {path.relative_to(ROOT).as_posix() for path in artifact_paths()}
    basis_hashes = {
        relative: digest for relative, digest in source_hashes.items() if relative not in generated
    }
    artifacts = {
        suffix.lstrip(".").replace(".", "_"): {
            "path": (ROOT / "programs/step5/step5d" / f"{PROGRAM}{suffix}").relative_to(ROOT).as_posix(),
            "sha256": sha256_file(ROOT / "programs/step5/step5d" / f"{PROGRAM}{suffix}"),
        }
        for suffix in (".script", ".txt", ".urp", ".numeric-sanity.json", ".deploy-manifest.json")
    }
    basis = {
        "schema": "step5d.autotune-v4/r006-source-closure-basis-v1",
        "program": PROGRAM,
        "release_contract": {
            "path": "config/step5d/autotune_v4_r006.json",
            "campaign_fingerprint": contract.campaign_fingerprint,
        },
        "parent_identity": {
            "lineage": "step5d_strict_rnn_autotune_v4_r005",
            "r004_r005_identity_preserved": True,
            "qualifications_imported": False,
            "sha256": dict(contract.parent_hashes),
        },
        "objective": dict(contract.raw["objective"]),
        "runtime": dict(contract.raw["runtime"]),
        "optimizer": dict(contract.raw["optimizer"]),
        "safety_boundary": dict(contract.raw["offline_boundary"]),
        "source_closure": {"file_count": len(basis_hashes), "sha256": basis_hashes},
    }
    return {
        "schema": "step5d.autotune-v4/r006-offline-closure-v1",
        "program": PROGRAM,
        "release_contract": {
            "path": "config/step5d/autotune_v4_r006.json",
            "campaign_fingerprint": contract.campaign_fingerprint,
        },
        "parent_identity": {
            "lineage": "step5d_strict_rnn_autotune_v4_r005",
            "r004_r005_identity_preserved": True,
            "qualifications_imported": False,
            "sha256": dict(contract.parent_hashes),
        },
        "objective": dict(contract.raw["objective"]),
        "runtime": dict(contract.raw["runtime"]),
        "optimizer": dict(contract.raw["optimizer"]),
        "artifacts": artifacts,
        "source_closure": {"file_count": len(source_hashes), "sha256": source_hashes},
        "content_address": {
            "algorithm": "SHA-256",
            "basis": "r006-source-closure-basis-v1 excludes contract-bound generated triplet bytes",
            "payload": basis,
            "sha256": sha256_bytes(canonical_bytes(basis)),
        },
        "safety_boundary": dict(contract.raw["offline_boundary"]),
        "claim_boundary": "offline source/package closure only; no live acceptance",
    }


def write(path: Path = OUTPUT) -> dict[str, object]:
    document = build_document()
    encoded = (json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    Path(path).write_bytes(encoded)
    return {"path": str(path), "sha256": hashlib.sha256(encoded).hexdigest(), "source_count": document["source_closure"]["file_count"], "live_evidence": False}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(list(argv) if argv is not None else None)
    print(json.dumps(write(args.output), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
