#!/usr/bin/env python3
"""Generate or check current TacDiffusion truth views.

The command only reads repository-local files and writes the canonical current
views.  Historical evidence JSON is intentionally outside its write set.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ur10e_vic.tacdiffusion.truth import (  # noqa: E402
    TruthContractError,
    check_current_views,
    generate_current_views,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify checked-in current views without writing them",
    )
    args = parser.parse_args(argv)
    try:
        if args.check:
            check_current_views(repo_root=ROOT)
            print("TacDiffusion current truth views are deterministic and current.")
        else:
            generate_current_views(repo_root=ROOT, write=True)
            print("Generated OFFLINE_READINESS.json and config/tacdiffusion_current_validation.json.")
    except TruthContractError as exc:
        print(f"truth contract rejected: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
