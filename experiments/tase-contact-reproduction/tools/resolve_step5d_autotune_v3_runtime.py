#!/usr/bin/python3.10
"""Read-only bootstrap resolver for exact governed Step5d interpreters."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from step5d_autotune_v3.runtime_installation import (
    PROFILES,
    RuntimeInstallationError,
    load_runtime_pointer,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        pointer = load_runtime_pointer()
    except RuntimeInstallationError as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "schema": "step5d.autotune-v3/runtime-resolution-v1",
                        "ok": False,
                        "reason_code": exc.reason_code,
                        "detail": exc.detail,
                    },
                    sort_keys=True,
                )
            )
        else:
            print(f"{exc.reason_code}: {exc.detail}")
        return 2
    if args.profile is not None and not args.json:
        print(pointer["profiles"][args.profile]["python_executable"])
        return 0
    print(
        json.dumps(
            {
                "schema": "step5d.autotune-v3/runtime-resolution-v1",
                "ok": True,
                **pointer,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
