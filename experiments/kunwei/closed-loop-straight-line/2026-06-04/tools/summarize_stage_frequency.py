#!/usr/bin/env python3
"""Summarize UR output-register stage and heartbeat cadence for a bridge CSV."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path


def summarize(csv_path: Path) -> dict:
    rows: list[dict] = []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            try:
                row["_t"] = float(row["t_monotonic_s"])
                row["_stage"] = float(row.get("ur_output_double_register_35") or "nan")
                row["_echo"] = float(row.get("ur_output_double_register_26") or "nan")
            except ValueError:
                continue
            rows.append(row)

    def rate_for(stage: float) -> dict:
        stage_rows = [
            row
            for row in rows
            if math.isfinite(row["_stage"]) and abs(row["_stage"] - stage) < 0.05
        ]
        if len(stage_rows) < 2:
            return {
                "stage": stage,
                "duration_s": 0.0,
                "rtde_rows": len(stage_rows),
                "rtde_row_rate_hz": None,
                "echo_transitions": 0,
                "echo_rate_hz": None,
            }
        duration = stage_rows[-1]["_t"] - stage_rows[0]["_t"]
        transitions = 0
        previous = stage_rows[0]["_echo"]
        for row in stage_rows[1:]:
            current = row["_echo"]
            if math.isfinite(current) and current != previous:
                transitions += 1
                previous = current
        return {
            "stage": stage,
            "duration_s": duration,
            "rtde_rows": len(stage_rows),
            "rtde_row_rate_hz": (len(stage_rows) - 1) / duration if duration > 0 else None,
            "echo_transitions": transitions,
            "echo_rate_hz": transitions / duration if duration > 0 else None,
        }

    total_duration = rows[-1]["_t"] - rows[0]["_t"] if len(rows) >= 2 else 0.0
    return {
        "ok": True,
        "bridge_csv": str(csv_path),
        "total_rows": len(rows),
        "bridge_write_rate_hz": (len(rows) - 1) / total_duration if total_duration > 0 else None,
        "rtde_output_logging_rate_hz": (len(rows) - 1) / total_duration if total_duration > 0 else None,
        "stage24_search_echo_rate": rate_for(24.0),
        "stage24_2_near_search_echo_rate": rate_for(24.2),
        "stage24_4_soft_acquisition_echo_rate": rate_for(24.4),
        "stage25_ft_line_control_echo_rate": rate_for(25.0),
        "stage26_unload_echo_rate": rate_for(26.0),
        "stage27_retract_echo_rate": rate_for(27.0),
        "frequency_contract_note": "Stage25 echo rate is measured from ur_output_double_register_26 heartbeat transitions; do not treat it as internal servo-loop frequency.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bridge_csv", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output is None:
        args.output = args.bridge_csv.with_name("stage_frequency_summary.json")
    if not args.bridge_csv.exists():
        result = {"ok": False, "issue": "bridge CSV not found", "bridge_csv": str(args.bridge_csv)}
    else:
        result = summarize(args.bridge_csv)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
