#!/usr/bin/env python3
"""Run the contact freshness sensitivity replay on CSV or JSONL evidence."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from contact_benchmark_freshness import replay_freshness_sensitivity


def _read_rows(path: Path):
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        with path.open(encoding="utf-8") as stream:
            return [json.loads(line) for line in stream if line.strip()]
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="CSV or JSONL with observation_age_s")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = replay_freshness_sensitivity(_read_rows(args.input))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
