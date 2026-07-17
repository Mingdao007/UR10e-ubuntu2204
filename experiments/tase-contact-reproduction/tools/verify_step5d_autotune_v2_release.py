#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

from step5d_autotune_v2.release import ReleaseError, verify_release_config


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    try:
        report = verify_release_config(ROOT)
    except ReleaseError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
