#!/usr/bin/env python3
"""Run the offline contact-six complete-composition timing receipt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from contact_benchmark_protocol import CONTROLLERS
from contact_benchmark_timing import TimingConfig, run_writer_timing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ticks", type=int, default=1024)
    parser.add_argument("--controller", action="append", dest="controllers")
    args = parser.parse_args()
    controllers = tuple(args.controllers or CONTROLLERS)
    result = run_writer_timing(
        experiment_root=args.experiment_root,
        controllers=controllers,
        config=TimingConfig(ticks=args.ticks),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
