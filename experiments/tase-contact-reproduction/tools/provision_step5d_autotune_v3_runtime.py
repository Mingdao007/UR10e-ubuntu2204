#!/usr/bin/python3.10
"""Internal deploy-time provisioner for the governed Step5d V3 runtimes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from step5d_autotune_v3.runtime_installation import (
    RuntimeInstallationError,
    provision_runtime,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument("--controller-helper", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = provision_runtime(
            uv_executable=args.uv,
            controller_helper=args.controller_helper,
        )
    except RuntimeInstallationError as exc:
        payload = {
            "schema": "step5d.autotune-v3/runtime-provision-result-v1",
            "ok": False,
            "reason_code": exc.reason_code,
            "detail": exc.detail,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 2
    payload = {
        "schema": "step5d.autotune-v3/runtime-provision-result-v1",
        "ok": True,
        "bundle_id": result["bundle_id"],
        "attestation_sha256": result["attestation_sha256"],
        "profiles": result["profiles"],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
