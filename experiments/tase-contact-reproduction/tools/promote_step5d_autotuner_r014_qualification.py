#!/usr/bin/env python3
"""Promote R014 only after a complete owner-produced qualification bundle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from step5d_autotune_v4_r014.qualification import promote_small_qualification


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    result = promote_small_qualification(root, args.bundle)
    print(
        json.dumps(
            {
                "ok": True,
                "profile_path": str(result.qualified_profile.path),
                "profile_sha256": result.qualified_profile.sha256,
                "bundle_sha256": result.bundle_sha256,
                "evidence_sha256": list(result.evidence_sha256),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
