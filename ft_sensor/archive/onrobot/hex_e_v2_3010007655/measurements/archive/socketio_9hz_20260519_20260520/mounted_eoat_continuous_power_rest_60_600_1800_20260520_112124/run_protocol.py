#!/usr/bin/env python3
"""Run mounted-EOAT continuous-power rest segments for OnRobot HEX-E."""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


RUN_ROOT = Path(__file__).resolve().parent
EXP_ROOT = RUN_ROOT.parents[1]
HOST = "192.168.1.1"
REST_S = 1800
SEGMENTS = [
    {"name": "S00_60s", "duration_s": 60, "status_every_s": 30},
    {"name": "S01_600s", "duration_s": 600, "status_every_s": 120},
    {"name": "S02_3600s", "duration_s": 3600, "status_every_s": 600},
]
RESTS = [
    {"name": "R00_rest_30min", "after": "S00_60s", "duration_s": REST_S},
    {"name": "R01_rest_30min", "after": "S01_600s", "duration_s": REST_S},
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def append_event(event: dict) -> None:
    event = {"wall_time_utc": now_iso(), **event}
    with (RUN_ROOT / "protocol_events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(json.dumps(event, ensure_ascii=False), flush=True)


def write_state(state: dict) -> None:
    payload = {"updated_wall_time_utc": now_iso(), **state}
    (RUN_ROOT / "protocol_state.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def compute_box_version() -> str:
    with urllib.request.urlopen(f"http://{HOST}/version", timeout=3) as response:
        return response.read().decode("utf-8")


def run_checked(cmd: list[str], cwd: Path) -> None:
    append_event({"event": "command_start", "cmd": cmd})
    subprocess.run(cmd, cwd=str(cwd), check=True)
    append_event({"event": "command_done", "cmd": cmd})


def run_segment(segment: dict) -> None:
    name = segment["name"]
    duration_s = int(segment["duration_s"])
    out_dir = RUN_ROOT / "segments" / name
    out_dir.mkdir(parents=True, exist_ok=True)
    version = compute_box_version()
    append_event({"event": "segment_start", "segment": name, "duration_s": duration_s, "version": version})
    write_state({"phase": "segment", "segment": name, "duration_s": duration_s})
    cmd = [
        "python3",
        "tools/onrobot_socketio_logger.py",
        "--host",
        HOST,
        "--duration-s",
        str(duration_s),
        "--out-dir",
        str(out_dir),
        "--flush-every-s",
        "10",
        "--status-every-s",
        str(segment["status_every_s"]),
        "--baseline-s",
        "60",
        "--bin-s",
        "60",
    ]
    run_checked(cmd, EXP_ROOT)
    analyze = [
        "python3",
        "tools/analyze_onrobot_drift.py",
        str(out_dir),
        "--baseline-s",
        "60",
        "--bin-s",
        "60",
    ]
    run_checked(analyze, EXP_ROOT)
    append_event({"event": "segment_done", "segment": name})


def rest(rest: dict) -> None:
    name = rest["name"]
    duration_s = int(rest["duration_s"])
    rest_dir = RUN_ROOT / "rests" / name
    rest_dir.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    wall_start = now_iso()
    append_event({"event": "rest_start", "rest": name, "after": rest["after"], "duration_s": duration_s})
    while True:
        elapsed = time.monotonic() - start
        remaining = max(0.0, duration_s - elapsed)
        payload = {
            "rest": name,
            "after": rest["after"],
            "planned_duration_s": duration_s,
            "elapsed_s": elapsed,
            "remaining_s": remaining,
            "start_wall_time_utc": wall_start,
            "updated_wall_time_utc": now_iso(),
            "power_state": "continuous_on",
        }
        (rest_dir / "rest_state.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        write_state({"phase": "rest", "rest": name, "elapsed_s": elapsed, "remaining_s": remaining})
        if elapsed >= duration_s:
            break
        sleep_s = min(60.0, remaining)
        time.sleep(sleep_s)
    end_payload = {
        "rest": name,
        "after": rest["after"],
        "planned_duration_s": duration_s,
        "actual_duration_s": time.monotonic() - start,
        "start_wall_time_utc": wall_start,
        "end_wall_time_utc": now_iso(),
        "power_state": "continuous_on",
    }
    (rest_dir / "rest_summary.json").write_text(
        json.dumps(end_payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    append_event({"event": "rest_done", **end_payload})


def write_protocol_manifest() -> None:
    manifest = {
        "run_root": str(RUN_ROOT),
        "experiment_root": str(EXP_ROOT),
        "host": HOST,
        "protocol": "continuous_power_rest_recovery",
        "not_true_power_cycle_cooling": True,
        "ur10e_state": "off_not_read",
        "segments": SEGMENTS,
        "rests": RESTS,
        "read_only_policy": [
            "no_ur_motion",
            "no_ur_zero_ftsensor",
            "no_onrobot_zero",
            "no_onrobot_bias",
            "no_autocalib",
            "no_firmware",
            "no_config_write",
        ],
    }
    (RUN_ROOT / "protocol_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def main() -> int:
    (RUN_ROOT / "segments").mkdir(exist_ok=True)
    (RUN_ROOT / "rests").mkdir(exist_ok=True)
    write_protocol_manifest()
    append_event({"event": "protocol_start", "run_root": str(RUN_ROOT)})
    try:
        run_segment(SEGMENTS[0])
        rest(RESTS[0])
        run_segment(SEGMENTS[1])
        rest(RESTS[1])
        run_segment(SEGMENTS[2])
        run_checked(["python3", str(RUN_ROOT / "write_report.py")], EXP_ROOT)
    except Exception as exc:
        append_event({"event": "protocol_failed", "error": repr(exc)})
        write_state({"phase": "failed", "error": repr(exc)})
        raise
    append_event({"event": "protocol_done"})
    write_state({"phase": "done"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
