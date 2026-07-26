#!/usr/bin/env python3
"""Measure unchanged-mailbox read cost for the V3 production cache seam."""

from __future__ import annotations

import argparse
import gc
import json
import os
import statistics
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from step5d_autotune_v3.runtime_profile import IdentityCachedMailbox


SCHEMA = "step5d.autotune-v3/mailbox-timing/v1"


class DecodeDelegate:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.decode_count = 0

    def read_latest(self) -> Any:
        self.decode_count += 1
        with self.path.open("rb") as handle:
            return json.load(handle)


def _distribution(samples_ns: list[int]) -> dict[str, float]:
    ordered = sorted(samples_ns)

    def percentile(value: float) -> float:
        return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * value))] / 1000.0

    return {
        "p50_us": statistics.median(ordered) / 1000.0,
        "p95_us": percentile(0.95),
        "p99_us": percentile(0.99),
        "p999_us": percentile(0.999),
        "max_us": max(ordered) / 1000.0,
    }


def _samples(function: Any, iterations: int) -> list[int]:
    result: list[int] = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        function()
        result.append(time.perf_counter_ns() - started)
    return result


def benchmark(iterations: int) -> dict[str, Any]:
    if iterations < 1000:
        raise ValueError("iterations must be at least 1000")
    with tempfile.TemporaryDirectory(prefix="step5d-v3-mailbox-") as directory:
        path = Path(directory) / "command.json"
        path.write_text(
            json.dumps(
                {
                    "schema": "synthetic-timing-only",
                    "packet": {"command": 0, "sequence": 0},
                    "padding": "x" * 4096,
                },
                sort_keys=True,
            ),
            encoding="ascii",
        )
        baseline_delegate = DecodeDelegate(path)
        cached_delegate = DecodeDelegate(path)
        cached = IdentityCachedMailbox(cached_delegate)
        cached.read_latest()
        for _ in range(100):
            baseline_delegate.read_latest()
            cached.read_latest()
        gc_enabled = gc.isenabled()
        gc.disable()
        try:
            baseline_samples = _samples(baseline_delegate.read_latest, iterations)
            cached_samples = _samples(cached.read_latest, iterations)
        finally:
            if gc_enabled:
                gc.enable()
    baseline = _distribution(baseline_samples)
    cached_result = _distribution(cached_samples)
    return {
        "schema": SCHEMA,
        "ok": cached_result["p99_us"] < baseline["p99_us"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "iterations": iterations,
        "fixture_bytes": 4096,
        "baseline_full_decode": baseline,
        "v3_identity_cached_unchanged": cached_result,
        "p99_saved_us": baseline["p99_us"] - cached_result["p99_us"],
        "cached_delegate_decodes": cached_delegate.decode_count,
        "cache_policy": "full decode only after dev/inode/size/mtime_ns identity change",
        "claim_boundary": "host synthetic timing; not a real-time scheduling guarantee",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=50000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    payload = benchmark(args.iterations)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
