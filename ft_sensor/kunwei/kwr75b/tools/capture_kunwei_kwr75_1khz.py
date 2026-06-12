#!/usr/bin/env python3
"""Interruptible Kunwei KWR75/KWR75B 1 kHz capture.

Manual basis:
- 0x48 AA 0D 0A starts converted-result streaming at 1 kHz.
- streamed frames are 28 bytes: 0x48/0x49, 0xAA, six float32 values, 0D 0A.
- 0x43 AA 0D 0A stops data conversion and sending.

The script does not send configuration writes to UDP 5152.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import signal
import socket
import struct
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


START_STREAM = bytes.fromhex("48 AA 0D 0A")
STOP_STREAM = bytes.fromhex("43 AA 0D 0A")
FIELDS = ("Fx_kg_manual", "Fy_kg_manual", "Fz_kg_manual", "Mx_kg_m_manual", "My_kg_m_manual", "Mz_kg_m_manual")
FORCE_KG_TO_N = 9.80665
MOMENT_KG_M_TO_NM = 9.80665


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def default_output_dir() -> Path:
    return (
        Path("/home/andy/ur10e_ros2_ws/ft_sensor/kunwei/kwr75b/measurements")
        / f"{datetime.now():%Y%m%d}"
        / f"{datetime.now():%H%M%S}"
    )


def write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def parse_frame(frame: bytes) -> tuple[float, float, float, float, float, float]:
    if len(frame) != 28:
        raise ValueError(f"expected 28 bytes, got {len(frame)}")
    if frame[0] not in (0x48, 0x49) or frame[1] != 0xAA or frame[-2:] != b"\r\n":
        raise ValueError("invalid KWR75 frame boundary")
    return struct.unpack("<ffffff", frame[2:26])


def pop_frames(buffer: bytearray, expected_start: int | None) -> tuple[list[bytes], int]:
    frames: list[bytes] = []
    dropped = 0
    while True:
        if len(buffer) < 28:
            break
        start_idx = -1
        for idx in range(0, len(buffer) - 1):
            if buffer[idx + 1] == 0xAA and buffer[idx] in (0x48, 0x49):
                if expected_start is None or buffer[idx] == expected_start:
                    start_idx = idx
                    break
        if start_idx < 0:
            keep = buffer[-1:]
            dropped += max(0, len(buffer) - len(keep))
            buffer[:] = keep
            break
        if start_idx:
            dropped += start_idx
            del buffer[:start_idx]
        if len(buffer) < 28:
            break
        candidate = bytes(buffer[:28])
        if candidate[-2:] == b"\r\n":
            frames.append(candidate)
            del buffer[:28]
        else:
            dropped += 1
            del buffer[0]
    return frames, dropped


class RunningStats:
    def __init__(self) -> None:
        self.n = 0
        self.mean = 0.0
        self.m2 = 0.0
        self.min: float | None = None
        self.max: float | None = None
        self.first: float | None = None
        self.last: float | None = None

    def push(self, value: float) -> None:
        if self.first is None:
            self.first = value
        self.last = value
        self.min = value if self.min is None else min(self.min, value)
        self.max = value if self.max is None else max(self.max, value)
        self.n += 1
        delta = value - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (value - self.mean)

    def as_dict(self) -> dict[str, Any]:
        if not self.n:
            return {"samples": 0}
        variance = self.m2 / (self.n - 1) if self.n > 1 else 0.0
        return {
            "samples": self.n,
            "first": self.first,
            "last": self.last,
            "last_minus_first": None if self.first is None or self.last is None else self.last - self.first,
            "mean": self.mean,
            "std": math.sqrt(variance),
            "min": self.min,
            "max": self.max,
        }


class CaptureState:
    def __init__(self) -> None:
        self.rows = 0
        self.bytes_received = 0
        self.packets_received = 0
        self.dropped_sync_bytes = 0
        self.parse_errors = 0
        self.command_counts: Counter[str] = Counter()
        self.intervals = RunningStats()
        self.axes = {field: RunningStats() for field in FIELDS}
        self.first_t_monotonic: float | None = None
        self.last_t_monotonic: float | None = None

    def push(self, frame: bytes, values: tuple[float, ...], t_mono: float) -> None:
        if self.last_t_monotonic is not None:
            self.intervals.push(t_mono - self.last_t_monotonic)
        if self.first_t_monotonic is None:
            self.first_t_monotonic = t_mono
        self.last_t_monotonic = t_mono
        self.rows += 1
        self.command_counts[f"0x{frame[0]:02X}"] += 1
        for field, value in zip(FIELDS, values):
            self.axes[field].push(value)

    def as_dict(self, start_mono: float, stop_reason: str) -> dict[str, Any]:
        now_mono = time.monotonic()
        first_last = (
            None
            if self.first_t_monotonic is None or self.last_t_monotonic is None
            else self.last_t_monotonic - self.first_t_monotonic
        )
        interval = self.intervals.as_dict()
        mean_dt = interval.get("mean")
        return {
            "stop_reason": stop_reason,
            "elapsed_wall_s": now_mono - start_mono,
            "samples": self.rows,
            "duration_first_last_s": first_last,
            "rate_hz_by_first_last": (self.rows - 1) / first_last if first_last and self.rows > 1 else None,
            "mean_dt_ms": mean_dt * 1000.0 if mean_dt is not None else None,
            "min_dt_ms": interval.get("min") * 1000.0 if interval.get("min") is not None else None,
            "max_dt_ms": interval.get("max") * 1000.0 if interval.get("max") is not None else None,
            "bytes_received": self.bytes_received,
            "packets_received": self.packets_received,
            "dropped_sync_bytes": self.dropped_sync_bytes,
            "parse_errors": self.parse_errors,
            "command_counts": dict(self.command_counts),
            "axis_stats_manual_units": {field: stats.as_dict() for field, stats in self.axes.items()},
        }


def open_transport(args: argparse.Namespace) -> tuple[socket.socket, socket.socket | None, tuple[str, int] | None]:
    if args.transport == "tcp-client":
        sock = socket.create_connection((args.sensor_ip, args.sensor_port), timeout=args.connect_timeout_s)
        sock.settimeout(1.0)
        return sock, None, None
    if args.transport == "tcp-listen":
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((args.bind_ip, args.local_port))
        server.listen(1)
        server.settimeout(args.connect_timeout_s)
        conn, addr = server.accept()
        conn.settimeout(1.0)
        return conn, server, addr
    if args.transport == "udp":
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((args.bind_ip, args.local_port))
        sock.settimeout(1.0)
        return sock, None, (args.sensor_ip, args.sensor_port)
    raise ValueError(args.transport)


def send_command(sock: socket.socket, transport: str, command: bytes, udp_peer: tuple[str, int] | None) -> None:
    if transport == "udp":
        if udp_peer is None:
            raise ValueError("UDP peer is required")
        sock.sendto(command, udp_peer)
    else:
        sock.sendall(command)


def receive_chunk(sock: socket.socket, transport: str) -> bytes:
    if transport == "udp":
        data, _addr = sock.recvfrom(8192)
        return data
    return sock.recv(8192)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=("tcp-listen", "tcp-client", "udp"), default="tcp-listen")
    parser.add_argument("--sensor-ip", default="192.168.50.25")
    parser.add_argument("--sensor-port", type=int, default=8886)
    parser.add_argument("--bind-ip", default="192.168.50.26")
    parser.add_argument("--local-port", type=int, default=8886)
    parser.add_argument("--duration-s", type=float, default=24 * 60 * 60)
    parser.add_argument("--checkpoint-interval-s", type=float, default=900.0)
    parser.add_argument("--connect-timeout-s", type=float, default=120.0)
    parser.add_argument("--output-dir", type=Path, default=default_output_dir())
    parser.add_argument("--flush-every", type=int, default=1000)
    parser.add_argument("--no-start-command", action="store_true")
    parser.add_argument("--no-stop-command", action="store_true")
    args = parser.parse_args(argv)

    if args.duration_s <= 0:
        raise SystemExit("--duration-s must be positive")
    if args.checkpoint_interval_s <= 0:
        raise SystemExit("--checkpoint-interval-s must be positive")
    if args.flush_every <= 0:
        raise SystemExit("--flush-every must be positive")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.output_dir / "data.csv"
    raw_path = args.output_dir / "raw_frames.bin"
    checkpoint_path = args.output_dir / "checkpoint.json"
    summary_path = args.output_dir / "summary.json"
    metadata_path = args.output_dir / "metadata.json"

    stop_reason = {"value": "duration"}
    stop_requested = {"value": False}

    def signal_handler(signum: int, _frame: Any) -> None:
        stop_reason["value"] = f"signal_{signum}"
        stop_requested["value"] = True

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    metadata = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "manual_basis": {
            "stream_command_hex": START_STREAM.hex(" ").upper(),
            "stop_command_hex": STOP_STREAM.hex(" ").upper(),
            "frame_bytes": 28,
            "manual_stream_rate_hz": 1000,
            "raw_force_units": "Kg as written in manual",
            "raw_moment_units": "Kg*m as written in manual",
            "configuration_write_not_used": True,
            "configuration_write_prefix_not_sent": "EE AA",
            "port_note": "UDP 5152/5153 are manual configuration ports. If TCP 5152 is selected here, it is used only as the observed data socket for 0x48/0x43 sensor commands, never for 0xEE configuration writes.",
            "latency_timer_note": "Manual page about latency=1 applies to USB-serial ttyUSB latency_timer; current capture uses Ethernet socket unless a ttyUSB device is present.",
        },
        "args": vars(args) | {"output_dir": str(args.output_dir)},
        "paths": {
            "csv": str(csv_path),
            "raw_frames": str(raw_path),
            "checkpoint": str(checkpoint_path),
            "summary": str(summary_path),
            "metadata": str(metadata_path),
        },
    }
    write_json(metadata_path, metadata)

    state = CaptureState()
    start_mono = time.monotonic()
    next_checkpoint = start_mono + args.checkpoint_interval_s
    expected_start = 0x48 if not args.no_start_command else None
    buffer = bytearray()
    sock: socket.socket | None = None
    server_sock: socket.socket | None = None
    udp_peer: tuple[str, int] | None = None

    def checkpoint() -> None:
        write_json(
            checkpoint_path,
            {
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "checkpoint_interval_s": args.checkpoint_interval_s,
                "summary_so_far": state.as_dict(start_mono, stop_reason["value"]),
                "paths": metadata["paths"],
            },
        )

    try:
        sock, server_sock, peer = open_transport(args)
        if args.transport == "udp":
            udp_peer = peer
        else:
            metadata["connected_peer"] = str(peer or sock.getpeername())
            write_json(metadata_path, metadata)

        if not args.no_start_command:
            send_command(sock, args.transport, START_STREAM, udp_peer)

        with csv_path.open("w", newline="") as csv_file, raw_path.open("wb") as raw_file:
            writer = csv.writer(csv_file)
            writer.writerow(
                [
                    "sample_index",
                    "t_wall_ns",
                    "t_monotonic_s",
                    "command",
                    *FIELDS,
                    "Fx_N",
                    "Fy_N",
                    "Fz_N",
                    "Mx_Nm",
                    "My_Nm",
                    "Mz_Nm",
                    "frame_hex",
                ]
            )
            while not stop_requested["value"]:
                if time.monotonic() - start_mono >= args.duration_s:
                    stop_reason["value"] = "duration"
                    break
                try:
                    chunk = receive_chunk(sock, args.transport)
                except socket.timeout:
                    now = time.monotonic()
                    if now >= next_checkpoint:
                        checkpoint()
                        next_checkpoint = now + args.checkpoint_interval_s
                    continue
                if not chunk:
                    stop_reason["value"] = "socket_closed"
                    break
                state.bytes_received += len(chunk)
                state.packets_received += 1
                buffer.extend(chunk)
                frames, dropped = pop_frames(buffer, expected_start)
                state.dropped_sync_bytes += dropped
                for frame in frames:
                    try:
                        values = parse_frame(frame)
                    except ValueError:
                        state.parse_errors += 1
                        continue
                    t_mono = time.monotonic()
                    t_wall_ns = time.time_ns()
                    state.push(frame, values, t_mono)
                    raw_file.write(frame)
                    writer.writerow(
                        [
                            state.rows,
                            t_wall_ns,
                            f"{t_mono:.9f}",
                            f"0x{frame[0]:02X}",
                            *[f"{value:.9g}" for value in values],
                            f"{values[0] * FORCE_KG_TO_N:.9g}",
                            f"{values[1] * FORCE_KG_TO_N:.9g}",
                            f"{values[2] * FORCE_KG_TO_N:.9g}",
                            f"{values[3] * MOMENT_KG_M_TO_NM:.9g}",
                            f"{values[4] * MOMENT_KG_M_TO_NM:.9g}",
                            f"{values[5] * MOMENT_KG_M_TO_NM:.9g}",
                            frame.hex(),
                        ]
                    )
                    if state.rows % args.flush_every == 0:
                        csv_file.flush()
                        raw_file.flush()
                now = time.monotonic()
                if now >= next_checkpoint:
                    csv_file.flush()
                    raw_file.flush()
                    checkpoint()
                    next_checkpoint = now + args.checkpoint_interval_s
    finally:
        if sock is not None and not args.no_stop_command:
            try:
                send_command(sock, args.transport, STOP_STREAM, udp_peer)
            except OSError:
                pass
        if sock is not None:
            sock.close()
        if server_sock is not None:
            server_sock.close()

    checkpoint()
    summary = {
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "metadata_path": str(metadata_path),
        "paths": metadata["paths"],
        **state.as_dict(start_mono, stop_reason["value"]),
    }
    write_json(summary_path, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if state.rows > 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
