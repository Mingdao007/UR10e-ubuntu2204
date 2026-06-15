#!/usr/bin/env python3
"""Shared helpers for OnRobot HEX high-speed UDP experiments.

This module only defines packet formatting, parsing, CSV rows, and offline
summary helpers. Safety policy belongs in the script that imports it.
"""

from __future__ import annotations

import csv
import json
import socket
import statistics
import struct
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


HEADER = 0x1234
UDP_PORT = 49152

COMMAND_START = 0x0002
COMMAND_STOP = 0x0000
COMMAND_BIAS = 0x0042
COMMAND_FILTER = 0x0081
COMMAND_SPEED = 0x0082

BIASING_ON = 0xFF
BIASING_OFF = 0x00

FORCE_DIVISOR = 10000.0
TORQUE_DIVISOR = 100000.0

COMMAND_STRUCT = struct.Struct("!HHI")
RESPONSE_STRUCT = struct.Struct("!IIIiiiiii")
RESPONSE_SIZE = RESPONSE_STRUCT.size

RAW_FIELDS = ["fx_raw", "fy_raw", "fz_raw", "tx_raw", "ty_raw", "tz_raw"]
VALUE_FIELDS = ["fx_n", "fy_n", "fz_n", "tx_nm", "ty_nm", "tz_nm"]


@dataclass(frozen=True)
class UdpSample:
    sequence_number: int
    sample_counter: int
    status: int
    fx_raw: int
    fy_raw: int
    fz_raw: int
    tx_raw: int
    ty_raw: int
    tz_raw: int

    @property
    def fx_n(self) -> float:
        return self.fx_raw / FORCE_DIVISOR

    @property
    def fy_n(self) -> float:
        return self.fy_raw / FORCE_DIVISOR

    @property
    def fz_n(self) -> float:
        return self.fz_raw / FORCE_DIVISOR

    @property
    def tx_nm(self) -> float:
        return self.tx_raw / TORQUE_DIVISOR

    @property
    def ty_nm(self) -> float:
        return self.ty_raw / TORQUE_DIVISOR

    @property
    def tz_nm(self) -> float:
        return self.tz_raw / TORQUE_DIVISOR


def command_name(command: int) -> str:
    names = {
        COMMAND_START: "START",
        COMMAND_STOP: "STOP",
        COMMAND_BIAS: "BIAS",
        COMMAND_FILTER: "FILTER",
        COMMAND_SPEED: "SPEED",
    }
    return names.get(command, f"UNKNOWN_0x{command:04x}")


def pack_command(command: int, data: int) -> bytes:
    return COMMAND_STRUCT.pack(HEADER, command, data)


def send_command(sock: socket.socket, command: int, data: int, delay_s: float = 0.005) -> dict:
    payload = pack_command(command, data)
    sock.send(payload)
    if delay_s > 0:
        time.sleep(delay_s)
    return {
        "command": command_name(command),
        "command_hex": f"0x{command:04x}",
        "data": data,
        "sent_wall": datetime.now().isoformat(timespec="milliseconds"),
    }


def parse_response(payload: bytes) -> UdpSample:
    if len(payload) != RESPONSE_SIZE:
        raise ValueError(f"expected {RESPONSE_SIZE} bytes, got {len(payload)}")
    values = RESPONSE_STRUCT.unpack(payload)
    return UdpSample(*values)


def make_udp_socket(host: str, port: int, timeout_s: float) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(timeout_s)
    sock.connect((host, port))
    return sock


def receive_sample(sock: socket.socket) -> UdpSample:
    payload = sock.recv(RESPONSE_SIZE)
    return parse_response(payload)


def sample_to_row(index: int, t_s: float, recv_latency_ms: float, sample: UdpSample) -> dict:
    return {
        "sample_index": index,
        "t_s": f"{t_s:.9f}",
        "recv_latency_ms": f"{recv_latency_ms:.6f}",
        "sequence_number": sample.sequence_number,
        "sample_counter": sample.sample_counter,
        "status": sample.status,
        "fx_n": f"{sample.fx_n:.8f}",
        "fy_n": f"{sample.fy_n:.8f}",
        "fz_n": f"{sample.fz_n:.8f}",
        "tx_nm": f"{sample.tx_nm:.8f}",
        "ty_nm": f"{sample.ty_nm:.8f}",
        "tz_nm": f"{sample.tz_nm:.8f}",
        "fx_raw": sample.fx_raw,
        "fy_raw": sample.fy_raw,
        "fz_raw": sample.fz_raw,
        "tx_raw": sample.tx_raw,
        "ty_raw": sample.ty_raw,
        "tz_raw": sample.tz_raw,
    }


def csv_fieldnames() -> list[str]:
    return [
        "sample_index",
        "t_s",
        "recv_latency_ms",
        "sequence_number",
        "sample_counter",
        "status",
        *VALUE_FIELDS,
        *RAW_FIELDS,
    ]


def write_rows(csv_path: Path, rows: Iterable[dict]) -> None:
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_fieldnames())
        writer.writeheader()
        writer.writerows(rows)


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


def counter_delta(previous: int, current: int) -> tuple[int | None, bool, bool]:
    if current >= previous:
        return current - previous, False, False
    wrapped_delta = current + (2**32) - previous
    if wrapped_delta < 2**31:
        return wrapped_delta, True, False
    return None, False, True


def sequence_stats(values: list[int]) -> dict:
    if len(values) < 2:
        return {
            "first": values[0] if values else None,
            "last": values[-1] if values else None,
            "lost_estimate": None,
            "duplicate_or_zero_delta_count": 0,
            "backward_count": 0,
            "wrap_count": 0,
            "max_forward_delta": None,
        }

    lost = 0
    duplicate_or_zero = 0
    backward = 0
    wraps = 0
    deltas: list[int] = []
    for previous, current in zip(values, values[1:]):
        delta, wrapped, went_backward = counter_delta(previous, current)
        if went_backward or delta is None:
            backward += 1
            continue
        if wrapped:
            wraps += 1
        if delta == 0:
            duplicate_or_zero += 1
        elif delta > 1:
            lost += delta - 1
        deltas.append(delta)

    return {
        "first": values[0],
        "last": values[-1],
        "lost_estimate": lost,
        "duplicate_or_zero_delta_count": duplicate_or_zero,
        "backward_count": backward,
        "wrap_count": wraps,
        "max_forward_delta": max(deltas) if deltas else None,
    }


def timing_stats(elapsed_s: list[float], recv_latencies_ms: list[float]) -> dict:
    if len(elapsed_s) >= 2:
        dts = [b - a for a, b in zip(elapsed_s, elapsed_s[1:])]
        duration = elapsed_s[-1] - elapsed_s[0]
        mean_dt = statistics.fmean(dts)
    else:
        dts = []
        duration = None
        mean_dt = None

    return {
        "samples": len(elapsed_s),
        "first_t_s": elapsed_s[0] if elapsed_s else None,
        "last_t_s": elapsed_s[-1] if elapsed_s else None,
        "duration_first_last_s": duration,
        "samples_per_duration_hz": len(elapsed_s) / duration if duration else None,
        "intervals_per_duration_hz": (len(elapsed_s) - 1) / duration if duration else None,
        "one_over_mean_dt_hz": 1.0 / mean_dt if mean_dt else None,
        "mean_dt_ms": mean_dt * 1000.0 if mean_dt else None,
        "median_dt_ms": statistics.median(dts) * 1000.0 if dts else None,
        "p95_dt_ms": percentile(dts, 0.95) * 1000.0 if dts else None,
        "p99_dt_ms": percentile(dts, 0.99) * 1000.0 if dts else None,
        "min_dt_ms": min(dts) * 1000.0 if dts else None,
        "max_dt_ms": max(dts) * 1000.0 if dts else None,
        "mean_recv_latency_ms": statistics.fmean(recv_latencies_ms)
        if recv_latencies_ms
        else None,
        "median_recv_latency_ms": statistics.median(recv_latencies_ms)
        if recv_latencies_ms
        else None,
        "p95_recv_latency_ms": percentile(recv_latencies_ms, 0.95),
        "p99_recv_latency_ms": percentile(recv_latencies_ms, 0.99),
        "min_recv_latency_ms": min(recv_latencies_ms) if recv_latencies_ms else None,
        "max_recv_latency_ms": max(recv_latencies_ms) if recv_latencies_ms else None,
    }


def value_stats(samples: list[UdpSample]) -> dict:
    result: dict[str, dict[str, float] | None] = {}
    for field in VALUE_FIELDS:
        values = [float(getattr(sample, field)) for sample in samples]
        if not values:
            result[field] = None
            continue
        result[field] = {
            "mean": statistics.fmean(values),
            "min": min(values),
            "max": max(values),
            "first": values[0],
            "last": values[-1],
            "last_minus_first": values[-1] - values[0],
        }
    return result


def status_counts(samples: list[UdpSample]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for sample in samples:
        key = str(sample.status)
        counts[key] = counts.get(key, 0) + 1
    return counts


def write_summary(summary_path: Path, summary: dict) -> None:
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def summarize_run(
    *,
    samples: list[UdpSample],
    elapsed_s: list[float],
    recv_latencies_ms: list[float],
    errors: list[str],
    commands_sent: list[dict],
    csv_path: Path,
    summary_path: Path,
    host: str,
    port: int,
    requested_seconds: float,
    max_samples: int,
    started_wall: str,
    finished_wall: str,
    safety_boundary: list[str],
    extra: dict | None = None,
) -> dict:
    summary = {
        "host": host,
        "port": port,
        "requested_seconds": requested_seconds,
        "max_samples": max_samples,
        "started_wall": started_wall,
        "finished_wall": finished_wall,
        "csv_path": str(csv_path),
        "summary_path": str(summary_path),
        "commands_sent": commands_sent,
        "safety_boundary": safety_boundary,
        "scaling": {
            "force_divisor": FORCE_DIVISOR,
            "torque_divisor": TORQUE_DIVISOR,
            "source": "OnRobot vendor highspeed_udp.c Newton/Newton-meter mode",
        },
        "errors": errors,
        "status_counts": status_counts(samples),
        "sequence_number": sequence_stats([sample.sequence_number for sample in samples]),
        "sample_counter": sequence_stats([sample.sample_counter for sample in samples]),
        "force_torque": value_stats(samples),
        **timing_stats(elapsed_s, recv_latencies_ms),
    }
    if extra:
        summary.update(extra)
    return summary
