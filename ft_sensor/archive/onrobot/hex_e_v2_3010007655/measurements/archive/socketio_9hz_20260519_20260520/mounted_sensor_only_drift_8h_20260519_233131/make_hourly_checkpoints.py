#!/usr/bin/env python3
"""Create hourly OnRobot drift checkpoints from a running main CSV."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
from pathlib import Path


def read_rows(csv_path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        return list(reader.fieldnames or []), rows


def row_time(row: dict[str, str]) -> float | None:
    try:
        return float(row.get("t_s", ""))
    except ValueError:
        return None


def write_checkpoint(
    fieldnames: list[str],
    rows: list[dict[str, str]],
    checkpoint_dir: Path,
) -> None:
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    csv_path = checkpoint_dir / "raw_wrench.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-root", required=True)
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--main-dir", required=True)
    parser.add_argument("--hours", type=int, default=8)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    experiment_root = Path(args.experiment_root).resolve()
    run_root = Path(args.run_root).resolve()
    main_dir = Path(args.main_dir).resolve()
    raw_csv = main_dir / "raw_wrench.csv"
    if not raw_csv.exists():
        print(f"checkpoint_wait raw_missing {raw_csv}", flush=True)
        return 0

    fieldnames, rows = read_rows(raw_csv)
    if not rows:
        print("checkpoint_wait rows=0", flush=True)
        return 0

    times = [t for t in (row_time(row) for row in rows) if t is not None]
    if not times:
        print("checkpoint_wait no_t_s", flush=True)
        return 0
    max_t = max(times)

    checkpoints_root = run_root / "checkpoints"
    checkpoints_root.mkdir(parents=True, exist_ok=True)
    created = []
    for hour in range(1, args.hours + 1):
        threshold = hour * 3600.0
        if max_t < threshold:
            continue
        name = f"{hour:02d}h"
        checkpoint_dir = checkpoints_root / name
        summary_path = checkpoint_dir / "summary.json"
        if summary_path.exists() and not args.force:
            continue
        subset = [row for row in rows if (row_time(row) is not None and row_time(row) <= threshold)]
        if not subset:
            continue
        write_checkpoint(fieldnames, subset, checkpoint_dir)
        subprocess.run(
            [
                "python3",
                str(experiment_root / "tools" / "analyze_onrobot_drift.py"),
                str(checkpoint_dir),
                "--baseline-s",
                "600",
                "--bin-s",
                "600",
            ],
            cwd=str(experiment_root),
            check=True,
        )
        created.append({"name": name, "rows": len(subset), "threshold_s": threshold})

    manifest = {
        "main_dir": str(main_dir),
        "max_t_s": max_t,
        "rows_seen": len(rows),
        "created_or_updated": created,
    }
    (checkpoints_root / "checkpoint_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    if created:
        print("checkpoint_created " + json.dumps(created), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
