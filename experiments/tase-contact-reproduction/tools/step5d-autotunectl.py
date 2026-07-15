#!/usr/bin/env python3
"""Location-independent executable for the Step5d autotune v2 CLI."""

from __future__ import annotations

import sys
from pathlib import Path


TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from step5d_autotune_v2.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
