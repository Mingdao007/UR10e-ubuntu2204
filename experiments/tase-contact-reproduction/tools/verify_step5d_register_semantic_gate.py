#!/usr/bin/env python3
"""Verify Step5d Stage25.0 does not consume stale preload registers as qdot."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QDOT_CAP_RAD_S = 0.05
DEFAULT_LAYOUT_TAG = 521.0
DEFAULT_FIRST_ROWS = 4


def fail(message: str) -> None:
    raise RuntimeError(f"Step5d register semantic gate failed: {message}")


def parse_float(value: str | None, default: float | None = None) -> float | None:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except ValueError:
        return default


def row_stage(row: dict[str, str]) -> float | None:
    return parse_float(row.get("ur_output_double_register_35"), parse_float(row.get("_step4e_line_stage_s")))


def verify_csv(
    csv_path: Path,
    *,
    qdot_cap_rad_s: float = DEFAULT_QDOT_CAP_RAD_S,
    layout_tag: float = DEFAULT_LAYOUT_TAG,
    first_rows: int = DEFAULT_FIRST_ROWS,
) -> dict[str, Any]:
    if not csv_path.is_file():
        fail(f"missing CSV: {csv_path}")

    stage25_rows: list[dict[str, Any]] = []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"ur_output_double_register_35", "ur_output_double_register_46"}
        required.update(f"ur_output_double_register_{idx}" for idx in range(36, 42))
        missing = sorted(required - set(reader.fieldnames or []))
        if missing:
            fail(f"CSV is missing required echo columns: {missing}")
        for row in reader:
            stage = row_stage(row)
            if stage is None or abs(stage - 25.0) > 1e-6:
                continue
            qdot_echo = [parse_float(row.get(f"ur_output_double_register_{idx}"), 0.0) or 0.0 for idx in range(36, 42)]
            layout_echo = parse_float(row.get("ur_output_double_register_46"), 0.0) or 0.0
            stage25_rows.append(
                {
                    "write_index": row.get("write_index"),
                    "stage": stage,
                    "qdot_echo": qdot_echo,
                    "qdot_echo_max_abs": max(abs(value) for value in qdot_echo),
                    "layout_echo": layout_echo,
                }
            )
            if len(stage25_rows) >= first_rows:
                break

    if not stage25_rows:
        fail("no Stage25.0 rows were found")
    offending = [
        row
        for row in stage25_rows
        if row["qdot_echo_max_abs"] > qdot_cap_rad_s + 1e-9
        or abs(row["layout_echo"] - layout_tag) <= 1e-6
    ]
    if offending:
        fail(
            "Stage25.0 begins with stale preload-layout echo: "
            + json.dumps(offending, sort_keys=True)
        )
    return {
        "ok": True,
        "csv": str(csv_path),
        "checked_stage25_rows": stage25_rows,
        "qdot_cap_rad_s": qdot_cap_rad_s,
        "forbidden_layout_tag": layout_tag,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--qdot-cap-rad-s", type=float, default=DEFAULT_QDOT_CAP_RAD_S)
    parser.add_argument("--layout-tag", type=float, default=DEFAULT_LAYOUT_TAG)
    parser.add_argument("--first-rows", type=int, default=DEFAULT_FIRST_ROWS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    csv_path = args.csv
    if not csv_path.is_absolute():
        csv_path = EXPERIMENT_ROOT / csv_path
    result = verify_csv(
        csv_path,
        qdot_cap_rad_s=args.qdot_cap_rad_s,
        layout_tag=args.layout_tag,
        first_rows=args.first_rows,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"[step5d-register-gate] passed: {csv_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc))
        raise SystemExit(24)
