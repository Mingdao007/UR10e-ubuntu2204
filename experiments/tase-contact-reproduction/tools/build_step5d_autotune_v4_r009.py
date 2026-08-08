#!/usr/bin/env python3
"""Build an R009 TP triplet into an explicitly supplied offline directory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable

from step5d_autotune_v4_r009.identity import (
    R009BehaviorManifest,
    build_behavior_manifest_from_r006,
)
from step5d_autotune_v4_r009.tp import build_triplet


def build(
    output_dir: Path,
    *,
    behavior_manifest: R009BehaviorManifest | None = None,
    manifest: R009BehaviorManifest | None = None,
    contract: object | None = None,
) -> dict[str, object]:
    """Build only the requested local triplet; no repository default is used."""

    supplied = [
        value for value in (behavior_manifest, manifest, contract) if value is not None
    ]
    if len(supplied) > 1:
        raise TypeError("R009 builder received multiple identity inputs")
    identity_input = supplied[0] if supplied else build_behavior_manifest_from_r006()
    paths = build_triplet(Path(output_dir), contract=identity_input)
    resolved_manifest = (
        identity_input.behavior_manifest
        if hasattr(identity_input, "behavior_manifest")
        else identity_input
    )
    if not isinstance(resolved_manifest, R009BehaviorManifest):
        raise TypeError("R009 builder identity input is not typed")
    return {
        "program": resolved_manifest.program,
        "output_dir": str(Path(output_dir)),
        "campaign_fingerprint": resolved_manifest.campaign_fingerprint,
        "behavior_manifest_sha256": resolved_manifest.behavior_manifest_sha256,
        "runtime_protocol": resolved_manifest.runtime_protocol,
        "artifacts": {key: str(value) for key, value in paths.items()},
        "controller_upload": False,
        "controller_readback": False,
        "live_evidence": False,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="explicit temporary/offline output directory",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    print(json.dumps(build(args.output_dir), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
