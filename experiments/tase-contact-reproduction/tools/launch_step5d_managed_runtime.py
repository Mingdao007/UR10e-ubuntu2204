#!/usr/bin/env python3
"""Version-neutral manifest launcher for managed control children."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_managed_runtime import ManagedRuntimeError, launch_manifest_route  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 2 or arguments[0] != "--manifest":
        raise SystemExit(
            "usage: launch_step5d_managed_runtime.py --manifest PATH <manifest route> [args ...]"
        )
    try:
        return launch_manifest_route(Path(arguments[1]), arguments[2:])
    except ManagedRuntimeError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
