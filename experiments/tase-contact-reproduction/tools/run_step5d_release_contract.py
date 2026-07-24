#!/usr/bin/python3.10
"""Canonical-shell worker for the no-network/no-trial release contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

from step5d_autotune_v3.release_contract import (
    ReleaseContractBlocked,
    ReleaseContractError,
    run_release_contract_check,
)
from step5d_autotune_v3.release_identity import (
    ReleaseIdentityError,
    load_local_release_candidate,
)


ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-root", type=Path, default=ROOT)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--release-candidate", type=Path)
    parser.add_argument("--manual", action="store_true")
    parser.add_argument("--reuse-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.manual and args.release_candidate is not None:
        _parser().error("--manual cannot combine with --release-candidate")
    started = time.monotonic()
    try:
        release = None
        release_candidate = None
        if args.release_candidate is not None:
            release, _descriptor = load_local_release_candidate(
                args.experiment_root,
                args.release_candidate,
            )
            release_candidate = str(
                args.release_candidate.expanduser().resolve(strict=True)
            )
        payload, certificate = run_release_contract_check(
            args.experiment_root,
            args.output_root,
            release_identity=release,
            subject_kind="manual_v2" if args.manual else "autotune_v3",
            reuse_only=args.reuse_only,
        )
    except ReleaseContractBlocked as exc:
        print(str(exc), file=sys.stderr)
        return 75
    except (OSError, ReleaseContractError, ReleaseIdentityError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    elapsed = time.monotonic() - started
    if elapsed > 3.0:
        print("release contract CLI exceeded three seconds", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "schema": "step5d.autotune-v3/release-contract-command-v1",
                "ok": True,
                "state": payload["state"],
                "subject_kind": payload["scope"]["subject_kind"],
                "release_manifest_sha256": payload["scope"][
                    "release_manifest_sha256"
                ],
                "release_candidate": release_candidate,
                "elapsed_s": elapsed,
                "certificate": certificate,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
