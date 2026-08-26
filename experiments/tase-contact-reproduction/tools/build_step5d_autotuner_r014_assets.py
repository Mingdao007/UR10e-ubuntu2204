#!/usr/bin/env python3
"""Materialize R014 content-addressed catalog, source closure, and profiles."""

from __future__ import annotations

import json
from pathlib import Path

from step5d_autotune_v4_r014.assets import build_assets
from step5d_autotune_v4_r014.profiles import ProfileRegistry


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    assets = build_assets(root)
    profiles = ProfileRegistry(root).install_defaults()
    print(
        json.dumps(
            {
                "assets": assets,
                "profiles": {
                    name: {"path": str(profile.path), "sha256": profile.sha256}
                    for name, profile in sorted(profiles.items())
                },
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
