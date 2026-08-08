#!/usr/bin/env python3
"""Thin r005 entrypoint for the lineage-neutral manifest launcher."""

from __future__ import annotations

import sys
from typing import Sequence


from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r005.contracts import load_contract  # noqa: E402
from step5d_managed_runtime import ManagedRuntimeError, launch_manifest_route  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if not arguments:
        raise SystemExit("usage: launch_step5d_autotune_v4_r005_control.py <manifest route> [args ...]")
    manifest = load_contract().runtime_manifest
    try:
        return launch_manifest_route(manifest.path, arguments)
    except ManagedRuntimeError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
