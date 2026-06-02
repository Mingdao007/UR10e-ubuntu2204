#!/usr/bin/env python3
"""Read OnRobot HEX Ethernet DAQ force data over the vendor TCP read path.

This script is based on the USB example:
  /media/andy/ONROBOT/Interfaces and Softwares/Ethernet/Example/tcp.c

Safety boundary: it sends only READCALIBRATIONINFO and READFT requests. It does
not send bias, filter, speed, zero, TCP, payload, URScript, or motion commands.
"""

from __future__ import annotations

import argparse
import csv
import json
import socket
import statistics
import struct
import time
from datetime import datetime
from pathlib import Path
from typing import BinaryIO


READ_FT = 0
READ_CALIBRATION_INFO = 1
COMMAND_SIZE = 20
CALIBRATION_SIZE = 24
FT_RESPONSE_SIZE = 16


def recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError(f"socket closed while reading {size} bytes")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def send_command(sock: socket.socket, command: int) -> None:
    sock.sendall(bytes([command]) + b"\x00" * (COMMAND_SIZE - 1))


def read_calibration(sock: socket.socket) -> dict:
    send_command(sock, READ_CALIBRATION_INFO)
    payload = recv_exact(sock, CALIBRATION_SIZE)
    values = struct.unpack("!HBBII6H", payload)
    header = values[0]
    if header != 0x1234:
        raise RuntimeError(f"unexpected calibration header: 0x{header:04x}")
    return {
        "header": header,
        "force_units": values[1],
        "torque_units": values[2],
        "counts_per_force": values[3],
        "counts_per_torque": values[4],
        "scale_factors": list(values[5:11]),
    }


def read_ft(sock: socket.socket, calibration: dict) -> dict:
    send_command(sock, READ_FT)
    payload = recv_exact(sock, FT_RESPONSE_SIZE)
    header, status, fx_raw, fy_raw, fz_raw, tx_raw, ty_raw, tz_raw = struct.unpack(
        "!HHhhhhhh", payload
    )
    if header != 0x1234:
        raise RuntimeError(f"unexpected FT header: 0x{header:04x}")

    cpf = calibration["counts_per_force"]
    cpt = calibration["counts_per_torque"]
    sf = calibration["scale_factors"]
    return {
        "status": status,
        "fx_raw": fx_raw,
        "fy_raw": fy_raw,
        "fz_raw": fz_raw,
        "tx_raw": tx_raw,
        "ty_raw": ty_raw,
        "tz_raw": tz_raw,
        "fx_n": fx_raw / cpf * sf[0],
        "fy_n": fy_raw / cpf * sf[1],
        "fz_n": fz_raw / cpf * sf[2],
        "tx_nm": tx_raw / cpt * sf[3],
        "ty_nm": ty_raw / cpt * sf[4],
        "tz_nm": tz_raw / cpt * sf[5],
    }


def write_header(csv_file: BinaryIO) -> csv.DictWriter:
    fieldnames = [
        "sample_index",
        "t_s",
        "latency_ms",
        "status",
        "fx_n",
        "fy_n",
        "fz_n",
        "tx_nm",
        "ty_nm",
        "tz_nm",
        "fx_raw",
        "fy_raw",
        "fz_raw",
        "tx_raw",
        "ty_raw",
        "tz_raw",
    ]
    writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
    writer.writeheader()
    return writer


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = (len(ordered) - 1) * p
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    fraction = rank - low
    return ordered[low] * (1.0 - fraction) + ordered[high] * fraction


def summarize(rows: int, elapsed: list[float], latencies_ms: list[float]) -> dict:
    if rows >= 2:
        dts = [b - a for a, b in zip(elapsed, elapsed[1:])]
        duration_first_last = elapsed[-1] - elapsed[0]
        rows_per_duration = rows / duration_first_last if duration_first_last > 0 else None
        intervals_per_duration = (rows - 1) / duration_first_last if duration_first_last > 0 else None
        mean_dt = statistics.fmean(dts)
    else:
        dts = []
        duration_first_last = None
        rows_per_duration = None
        intervals_per_duration = None
        mean_dt = None

    return {
        "samples": rows,
        "first_t_s": elapsed[0] if elapsed else None,
        "last_t_s": elapsed[-1] if elapsed else None,
        "duration_first_last_s": duration_first_last,
        "rows_per_duration_hz": rows_per_duration,
        "intervals_per_duration_hz": intervals_per_duration,
        "one_over_mean_dt_hz": (1.0 / mean_dt) if mean_dt else None,
        "mean_dt_ms": mean_dt * 1000.0 if mean_dt else None,
        "median_dt_ms": statistics.median(dts) * 1000.0 if dts else None,
        "p95_dt_ms": percentile(dts, 0.95) * 1000.0 if dts else None,
        "p99_dt_ms": percentile(dts, 0.99) * 1000.0 if dts else None,
        "min_dt_ms": min(dts) * 1000.0 if dts else None,
        "max_dt_ms": max(dts) * 1000.0 if dts else None,
        "mean_latency_ms": statistics.fmean(latencies_ms) if latencies_ms else None,
        "median_latency_ms": statistics.median(latencies_ms) if latencies_ms else None,
        "p95_latency_ms": percentile(latencies_ms, 0.95),
        "p99_latency_ms": percentile(latencies_ms, 0.99),
        "min_latency_ms": min(latencies_ms) if latencies_ms else None,
        "max_latency_ms": max(latencies_ms) if latencies_ms else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="192.168.1.1")
    parser.add_argument("--port", type=int, default=49151)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--timeout-s", type=float, default=2.0)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--prefix", default="onrobot_tcpdaq_readonly_60s")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = output_dir / f"{args.prefix}_{stamp}.csv"
    summary_path = output_dir / f"{args.prefix}_{stamp}_summary.json"

    calibration: dict | None = None
    elapsed: list[float] = []
    latencies_ms: list[float] = []
    errors: list[str] = []

    started_wall = datetime.now().isoformat(timespec="seconds")
    start_ns = time.perf_counter_ns()

    with socket.create_connection((args.host, args.port), timeout=args.timeout_s) as sock:
        sock.settimeout(args.timeout_s)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        calibration = read_calibration(sock)

        with csv_path.open("w", newline="") as csv_file:
            writer = write_header(csv_file)
            index = 0
            while True:
                before_ns = time.perf_counter_ns()
                if (before_ns - start_ns) / 1e9 >= args.seconds:
                    break
                try:
                    ft = read_ft(sock, calibration)
                except Exception as exc:  # noqa: BLE001 - preserve live-test error text
                    errors.append(f"{type(exc).__name__}: {exc}")
                    break
                after_ns = time.perf_counter_ns()

                t_s = (after_ns - start_ns) / 1e9
                latency_ms = (after_ns - before_ns) / 1e6
                elapsed.append(t_s)
                latencies_ms.append(latency_ms)
                writer.writerow(
                    {
                        "sample_index": index,
                        "t_s": f"{t_s:.9f}",
                        "latency_ms": f"{latency_ms:.6f}",
                        **ft,
                    }
                )
                index += 1

    summary = {
        "host": args.host,
        "port": args.port,
        "requested_seconds": args.seconds,
        "started_wall": started_wall,
        "finished_wall": datetime.now().isoformat(timespec="seconds"),
        "csv_path": str(csv_path),
        "source_example": "/media/andy/ONROBOT/Interfaces and Softwares/Ethernet/Example/tcp.c",
        "safety_boundary": [
            "READCALIBRATIONINFO command only before sampling",
            "READFT command only during sampling",
            "no bias/filter/speed/zero/TCP/payload/URScript/motion commands",
        ],
        "calibration": calibration,
        "errors": errors,
        **summarize(len(elapsed), elapsed, latencies_ms),
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
