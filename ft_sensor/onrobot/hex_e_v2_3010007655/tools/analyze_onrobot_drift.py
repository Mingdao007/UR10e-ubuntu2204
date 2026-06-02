#!/usr/bin/env python3
"""Rebuild summary, 10 min drift bins, and plot for an OnRobot logger run."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path


def load_logger_module(tools_dir: Path):
    logger_path = tools_dir / "onrobot_socketio_logger.py"
    spec = importlib.util.spec_from_file_location("onrobot_socketio_logger", logger_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {logger_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir")
    parser.add_argument("--baseline-s", type=float, default=600.0)
    parser.add_argument("--bin-s", type=float, default=600.0)
    args = parser.parse_args()

    run_dir = Path(args.run_dir).expanduser().resolve()
    module = load_logger_module(Path(__file__).resolve().parent)
    summary = module.summarize_run(run_dir, args.baseline_s, args.bin_s)
    print(f"summary: {run_dir / 'summary.json'}")
    print(f"bins: {run_dir / 'drift_10min_bins.csv'}")
    plots = summary.get("plots") or {"full": str(run_dir / "force_torque.png")}
    for name, path in plots.items():
        print(f"plot_{name}: {path}")
    print(f"rows: {summary['rows']} duration_s: {summary['duration_s']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
