#!/usr/bin/env python3
"""Create a bounded diagnostic plot from an immutable Step5d bridge CSV."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


TIME_FIELDS = (
    "t_monotonic_s",
    "elapsed_s",
    "monotonic_s",
    "time_s",
    "timestamp_s",
)
STAGE_FIELDS = ("ur_output_double_register_35", "robot_stage", "stage")
FORCE_FIELDS = ("_step4e_normal_load_n", "normal_load_n", "fz_n")


def finite(value: str | None) -> float | None:
    try:
        parsed = float(value) if value is not None else math.nan
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def first_present(fieldnames: Iterable[str], choices: Iterable[str]) -> str | None:
    available = set(fieldnames)
    return next((field for field in choices if field in available), None)


def read_series(path: Path, *, max_points: int) -> dict[str, object]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        time_field = first_present(fieldnames, TIME_FIELDS)
        stage_field = first_present(fieldnames, STAGE_FIELDS)
        force_field = first_present(fieldnames, FORCE_FIELDS)
        rows = list(reader)
    stride = max(1, math.ceil(len(rows) / max_points))
    sampled = rows[::stride]
    times: list[float] = []
    stages: list[float] = []
    forces: list[float] = []
    for index, row in enumerate(sampled):
        time_value = finite(row.get(time_field)) if time_field else None
        times.append(float(index * stride) if time_value is None else time_value)
        stage_value = finite(row.get(stage_field)) if stage_field else None
        stages.append(math.nan if stage_value is None else stage_value)
        force_value = finite(row.get(force_field)) if force_field else None
        forces.append(math.nan if force_value is None else force_value)
    return {
        "rows": len(rows),
        "sampled_rows": len(sampled),
        "stride": stride,
        "time_field": time_field,
        "stage_field": stage_field,
        "force_field": force_field,
        "times": times,
        "stages": stages,
        "forces": forces,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bridge_csv", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metadata-output", type=Path, required=True)
    parser.add_argument("--max-points", type=int, default=5000)
    args = parser.parse_args(argv)
    if args.max_points < 10:
        parser.error("--max-points must be >= 10")
    series = read_series(args.bridge_csv, max_points=args.max_points)
    times = series.pop("times")
    stages = series.pop("stages")
    forces = series.pop("forces")

    figure, axes = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
    axes[0].plot(times, stages, linewidth=0.8)
    axes[0].set_ylabel("stage")
    axes[0].grid(alpha=0.25)
    axes[1].plot(times, forces, linewidth=0.8)
    axes[1].set_ylabel("normal load (N)")
    axes[1].set_xlabel(str(series["time_field"] or "sample"))
    axes[1].grid(alpha=0.25)
    figure.tight_layout()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(args.output, dpi=140)
    plt.close(figure)

    metadata = {
        "schema_version": "step5d_bridge_diagnostic_plot_v1",
        "claim_class": "diagnostic_only",
        "source_csv": str(args.bridge_csv.resolve()),
        "plot": str(args.output.resolve()),
        **series,
    }
    args.metadata_output.parent.mkdir(parents=True, exist_ok=True)
    args.metadata_output.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
