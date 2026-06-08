#!/usr/bin/env python3
"""Send Kunwei KWR75B stop-stream command and verify quiet TCP output.

This sends only 0x43 AA 0D 0A. It does not send zero, tare, filter,
configuration, or start-stream commands.
"""

from __future__ import annotations

import argparse
import json
import select
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

STOP_STREAM = bytes.fromhex("43 AA 0D 0A")


def count_frame_markers(data: bytes) -> int:
    count = 0
    for i in range(max(0, len(data) - 1)):
        if data[i] in (0x48, 0x49) and data[i + 1] == 0xAA:
            count += 1
    return count


def send_stop_once(host: str, port: int, timeout_s: float) -> dict[str, Any]:
    started = time.monotonic()
    with socket.create_connection((host, port), timeout=timeout_s) as sock:
        sock.settimeout(timeout_s)
        sock.sendall(STOP_STREAM)
    return {"ok": True, "duration_s": time.monotonic() - started}


def probe_quiet(host: str, port: int, timeout_s: float, read_window_s: float) -> dict[str, Any]:
    started = time.monotonic()
    chunks = 0
    bytes_seen = 0
    marker_count = 0
    sample = b""
    with socket.create_connection((host, port), timeout=timeout_s) as sock:
        sock.setblocking(False)
        deadline = time.monotonic() + read_window_s
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            readable, _, _ = select.select([sock], [], [], min(0.05, remaining))
            if not readable:
                continue
            try:
                data = sock.recv(4096)
            except BlockingIOError:
                continue
            if not data:
                break
            chunks += 1
            bytes_seen += len(data)
            marker_count += count_frame_markers(data)
            if len(sample) < 64:
                sample += data[: 64 - len(sample)]
    return {
        "duration_s": time.monotonic() - started,
        "chunks": chunks,
        "bytes_seen": bytes_seen,
        "frame_markers_seen": marker_count,
        "sample_hex": sample.hex(" ").upper(),
        "quiet": bytes_seen == 0,
    }


def write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.50.25")
    parser.add_argument("--port", type=int, default=5152)
    parser.add_argument("--connect-timeout-s", type=float, default=1.0)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--interval-s", type=float, default=0.05)
    parser.add_argument("--read-window-s", type=float, default=0.35)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--json-only", action="store_true")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "host": args.host,
        "port": args.port,
        "stop_command_hex": STOP_STREAM.hex(" ").upper(),
        "safety_boundary": "stop-stream only; no zero/tare/filter/config/start command",
        "stop_attempts": [],
    }

    try:
        for _ in range(args.repeats):
            result["stop_attempts"].append(send_stop_once(args.host, args.port, args.connect_timeout_s))
            time.sleep(args.interval_s)
        result["probe"] = probe_quiet(args.host, args.port, args.connect_timeout_s, args.read_window_s)
        result["ok"] = bool(result["probe"]["quiet"])
        if not result["ok"]:
            result["issue"] = "stream bytes still arrived after repeated stop-stream commands"
    except OSError as exc:
        result["ok"] = False
        result["issue"] = f"socket error: {exc}"

    if args.output_json:
        write_json(args.output_json, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 3


if __name__ == "__main__":
    raise SystemExit(main())
