#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, "/home/andy/codex-private-skills/skills/ur10e-realsetup/scripts")
from _ur_common import RTDEClient  # noqa: E402


FIELDS = [
    "timestamp",
    "runtime_state",
    "robot_mode",
    "safety_mode",
    "speed_scaling",
    "actual_TCP_pose",
    "actual_TCP_speed",
    "output_double_register_24",
    "output_double_register_25",
    "output_double_register_26",
    "output_double_register_27",
    "output_double_register_28",
    "output_double_register_29",
    "output_double_register_30",
    "output_double_register_31",
    "output_double_register_32",
    "output_double_register_33",
]


def scalar_range(rows: list[dict[str, Any]], field: str) -> list[float] | None:
    values = [float(row[field]) for row in rows if row.get(field) is not None]
    if not values:
        return None
    return [min(values), max(values)]


def counts(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        key = str(row.get(field))
        out[key] = out.get(key, 0) + 1
    return out


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = ["local_elapsed_s", *FIELDS]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture UR RTDE output registers 24..29 for OnRobot Fx/Fy/Fz/Tx/Ty/Tz validation."
    )
    parser.add_argument("--host", default="192.168.1.18")
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--hz", type=float, default=125.0)
    parser.add_argument("--output-dir", default="/tmp/ur10e_demo_1_1_data_path_check")
    parser.add_argument("--prefix", default="onrobot_rtde_register_capture")
    args = parser.parse_args()

    outdir = Path(args.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    csv_path = outdir / f"{args.prefix}_{stamp}.csv"
    summary_path = outdir / f"{args.prefix}_{stamp}_summary.json"

    rows: list[dict[str, Any]] = []
    with RTDEClient(args.host, timeout=3.0) as client:
        client.negotiate()
        recipe_id, type_names = client.setup_outputs(args.hz, FIELDS)
        client.start()
        start = time.monotonic()
        while time.monotonic() - start < args.seconds:
            values = client.recv_recipe_sample(recipe_id, type_names)
            row = dict(zip(FIELDS, values))
            row["local_elapsed_s"] = time.monotonic() - start
            rows.append(row)

    write_csv(csv_path, rows)

    duration = rows[-1]["local_elapsed_s"] - rows[0]["local_elapsed_s"] if len(rows) > 1 else 0.0
    speeds = []
    for row in rows:
        speed = row.get("actual_TCP_speed")
        if isinstance(speed, list) and len(speed) >= 3:
            speeds.append(math.sqrt(speed[0] * speed[0] + speed[1] * speed[1] + speed[2] * speed[2]))

    summary = {
        "ok": True,
        "csv_path": str(csv_path),
        "summary_path": str(summary_path),
        "requested_hz": args.hz,
        "requested_seconds": args.seconds,
        "samples": len(rows),
        "local_duration_s": duration,
        "effective_hz": (len(rows) - 1) / duration if duration > 0 and len(rows) > 1 else None,
        "runtime_state_counts": counts(rows, "runtime_state"),
        "robot_mode_counts": counts(rows, "robot_mode"),
        "safety_mode_counts": counts(rows, "safety_mode"),
        "speed_scaling_range": scalar_range(rows, "speed_scaling"),
        "onrobot_register_mapping": {
            "output_double_register_24": "Fx",
            "output_double_register_25": "Fy",
            "output_double_register_26": "Fz",
            "output_double_register_27": "Tx",
            "output_double_register_28": "Ty",
            "output_double_register_29": "Tz",
            "output_double_register_30": "stop_reason",
            "output_double_register_31": "travel_m",
            "output_double_register_32": "abs_fz_delta",
            "output_double_register_33": "base_fz",
        },
        "register_staleness_warning": (
            "Output registers persist their last written values. Treat them as live OnRobot data only "
            "inside a program window that is actively writing Fx/Fy/Fz/Tx/Ty/Tz."
        ),
        "fz_register_range_n": scalar_range(rows, "output_double_register_26"),
        "stop_reason_register_range": scalar_range(rows, "output_double_register_30"),
        "travel_m_register_range": scalar_range(rows, "output_double_register_31"),
        "abs_fz_delta_register_range_n": scalar_range(rows, "output_double_register_32"),
        "base_fz_register_range_n": scalar_range(rows, "output_double_register_33"),
        "max_tcp_linear_speed_mm_s": max(speeds) * 1000.0 if speeds else None,
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
