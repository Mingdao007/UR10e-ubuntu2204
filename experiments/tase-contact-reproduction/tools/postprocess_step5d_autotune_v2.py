#!/usr/bin/env python3
"""Analyze one immutable Step5d v2 capture without performing a transfer."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from step5d_autotune_v2.postprocess import analyze_capture


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--trial-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = analyze_capture(
        capture=args.capture.resolve(),
        trial_id=args.trial_id,
        output_dir=args.output_dir.resolve(),
    )
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
