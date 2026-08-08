#!/usr/bin/env python3
"""Managed owner-gated r008 control launcher."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_managed_runtime import ManagedRuntimeError, launch_manifest_route  # noqa: E402
from step5d_autotune_v4_r009.quarantine import reject_r008_formal_resume  # noqa: E402


MANIFEST_PATH = ROOT / "config/step5d/autotune_v4_r008_runtime_manifest.json"


def main(argv: Sequence[str] | None = None) -> int:
    """Historical R008 control launch is permanently quarantined."""

    reject_r008_formal_resume("launch_step5d_autotune_v4_r008_control")
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if not arguments:
        raise SystemExit(
            "usage: launch_step5d_autotune_v4_r008_control.py <manifest route> [args ...]"
        )
    try:
        return launch_manifest_route(MANIFEST_PATH, arguments)
    except ManagedRuntimeError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
