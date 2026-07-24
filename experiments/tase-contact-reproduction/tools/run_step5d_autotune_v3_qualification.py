#!/usr/bin/python3.10
"""Canonical-shell-only worker for Step5d V3 formal qualification."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from step5d_autotune_v3.qualification import (
    RELEASE_CERTIFICATE_MISSING,
    QualificationError,
    exec_internal_shell_contract,
    require_canonical_launcher,
    run_endpoint_qualification,
)
from step5d_autotune_v3.release_identity import (
    ReleaseIdentityError,
    load_local_release_candidate,
)
from step5d_autotune_v3.runtime_installation import (
    RuntimeInstallationError,
    require_runtime_profile,
)


ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Internal Step5d V3 formal qualification worker"
    )
    parser.add_argument("--experiment-root", type=Path, default=ROOT)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--release-candidate", type=Path)
    parser.add_argument("--reuse-only", action="store_true")
    parser.add_argument(
        "--_exec-live-from-shell-contract",
        type=Path,
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args._exec_live_from_shell_contract is not None:
            if (
                args.output_root is not None
                or args.release_candidate is not None
                or args.reuse_only
            ):
                raise QualificationError(
                    "internal shell execution cannot accept qualification worker options"
                )
            exec_internal_shell_contract(
                args.experiment_root,
                args._exec_live_from_shell_contract,
            )
            raise AssertionError("internal shell execution unexpectedly returned")
        if args.output_root is None:
            raise QualificationError("--output-root is required")
        require_canonical_launcher(args.experiment_root)
        if not args.reuse_only:
            require_runtime_profile("control")
        release = None
        if args.release_candidate is not None:
            release, _descriptor = load_local_release_candidate(
                args.experiment_root,
                args.release_candidate,
            )
        payload, certificate = run_endpoint_qualification(
            args.experiment_root,
            args.output_root.resolve(),
            release_identity=release,
            reuse_only=args.reuse_only,
        )
    except (
        QualificationError,
        ReleaseIdentityError,
        RuntimeInstallationError,
    ) as exc:
        reason_code = (
            RELEASE_CERTIFICATE_MISSING
            if str(exc) == RELEASE_CERTIFICATE_MISSING
            else "CANONICAL_QUALIFICATION_REFUSED"
        )
        print(
            json.dumps(
                {
                    "ok": False,
                    "reason_code": reason_code,
                    "error": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 64
    print(
        json.dumps(
            {
                "schema": (
                    "step5d.autotune-v3/"
                    "release-certificate-worker-result-v1"
                ),
                "ok": payload["ok"],
                "release_manifest_sha256": (
                    payload.get("binding", {}).get("manifest_sha256")
                    if isinstance(payload.get("binding"), dict)
                    else None
                ),
                "reason_code": payload["reason_code"],
                "remaining_integration_seam": payload["remaining_integration_seam"],
                "certificate": certificate if payload["ok"] is True else None,
                "qualification_evidence": (
                    None if payload["ok"] is True else certificate
                ),
            },
            sort_keys=True,
        )
    )
    return 0 if payload["ok"] is True else 78


if __name__ == "__main__":
    raise SystemExit(main())
