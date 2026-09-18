#!/usr/bin/env python3
"""Run the six-controller equal-budget offline campaign."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from contact_benchmark_campaign import CampaignConfig, run_equal_budget_campaign


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--horizon-ticks", type=int, default=256)
    parser.add_argument("--seed", type=int, default=20260918)
    args = parser.parse_args()
    result = run_equal_budget_campaign(
        experiment_root=args.experiment_root,
        output_dir=args.output_dir,
        config=CampaignConfig(horizon_ticks=args.horizon_ticks, seed=args.seed),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
