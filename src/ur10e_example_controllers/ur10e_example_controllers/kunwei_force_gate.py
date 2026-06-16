from __future__ import annotations

import argparse
import json
import math
import socket
import struct
import time
from datetime import datetime
from pathlib import Path
from typing import Any


START_STREAM = bytes.fromhex("48 AA 0D 0A")
STOP_STREAM = bytes.fromhex("43 AA 0D 0A")
FIELDS = ("Fx_kg_manual", "Fy_kg_manual", "Fz_kg_manual", "Mx_kg_m_manual", "My_kg_m_manual", "Mz_kg_m_manual")
FORCE_KG_TO_N = 9.80665
MOMENT_KG_M_TO_NM = 9.80665


def parse_frame(frame: bytes) -> tuple[float, float, float, float, float, float]:
    if len(frame) != 28:
        raise ValueError(f"expected 28 bytes, got {len(frame)}")
    if frame[0] not in (0x48, 0x49) or frame[1] != 0xAA or frame[-2:] != b"\r\n":
        raise ValueError("invalid KWR75 frame boundary")
    return struct.unpack("<ffffff", frame[2:26])


def pop_frames(buffer: bytearray, expected_start: int = 0x48) -> tuple[list[bytes], int]:
    frames: list[bytes] = []
    dropped = 0
    while len(buffer) >= 28:
        start_idx = -1
        for index in range(0, len(buffer) - 1):
            if buffer[index] == expected_start and buffer[index + 1] == 0xAA:
                start_idx = index
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect a short Kunwei KWR75B pre-motion force gate sample.")
    parser.add_argument("--sensor-ip", default="192.168.50.25")
    parser.add_argument("--sensor-port", type=int, default=5152)
    parser.add_argument("--duration-s", type=float, default=1.0)
    parser.add_argument("--min-samples", type=int, default=20)
    parser.add_argument("--connect-timeout-s", type=float, default=3.0)
    parser.add_argument("--recv-timeout-s", type=float, default=0.25)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--raw-frames", type=Path, default=None)
    parser.add_argument("--no-start-command", action="store_true")
    parser.add_argument("--no-stop-command", action="store_true")
    args = parser.parse_args(argv)

    args.summary.parent.mkdir(parents=True, exist_ok=True)
    if args.raw_frames is not None:
        args.raw_frames.parent.mkdir(parents=True, exist_ok=True)

    result = _base_result(args)
    raw_handle = None
    sock: socket.socket | None = None
    try:
        if args.raw_frames is not None:
            raw_handle = args.raw_frames.open("wb")
        sock = socket.create_connection((args.sensor_ip, args.sensor_port), timeout=args.connect_timeout_s)
        sock.settimeout(args.recv_timeout_s)
        result["connected_peer"] = f"{args.sensor_ip}:{args.sensor_port}"
        if not args.no_start_command:
            sock.sendall(START_STREAM)
            result["stream_start_command_sent"] = True

        samples: list[tuple[float, ...]] = []
        sample_times: list[float] = []
        buffer = bytearray()
        deadline = time.monotonic() + args.duration_s
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(8192)
            except socket.timeout:
                continue
            if not chunk:
                result["failure_reason"] = "socket_closed"
                break
            result["bytes_received"] += len(chunk)
            result["packets_received"] += 1
            buffer.extend(chunk)
            frames, dropped = pop_frames(buffer)
            result["dropped_sync_bytes"] += dropped
            for frame in frames:
                try:
                    values = parse_frame(frame)
                except ValueError:
                    result["parse_errors"] += 1
                    continue
                if raw_handle is not None:
                    raw_handle.write(frame)
                samples.append(values)
                sample_times.append(time.monotonic())

        result.update(_sample_summary(samples, sample_times))
        if result["samples"] < args.min_samples:
            result["ok"] = False
            result["status"] = "insufficient_samples"
            result["failure_reason"] = result.get("failure_reason") or "no_or_insufficient_kunwei_samples"
            _write_json(args.summary, result)
            return 3
        result["ok"] = True
        result["status"] = "kunwei_ready"
        result["failure_reason"] = None
        _write_json(args.summary, result)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except OSError as exc:
        result["ok"] = False
        result["status"] = "connect_or_socket_error"
        result["failure_reason"] = f"{type(exc).__name__}: {exc}"
        _write_json(args.summary, result)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2
    finally:
        if raw_handle is not None:
            raw_handle.close()
        if sock is not None:
            if not args.no_stop_command:
                try:
                    sock.sendall(STOP_STREAM)
                    result["stream_stop_command_sent"] = True
                    _write_json(args.summary, result)
                except OSError:
                    pass
            sock.close()


def _base_result(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "gate": "kunwei_kwr75b_tcp_pre_motion_data_gate",
        "role": "primary_step5a_force_source_evidence",
        "ok": False,
        "status": "started",
        "sensor_endpoint": f"{args.sensor_ip}:{args.sensor_port}",
        "transport": "tcp-client",
        "bridge_process_status": "direct_tcp_no_bridge_process",
        "manual_basis": {
            "frame_bytes": 28,
            "stream_command_hex": START_STREAM.hex(" ").upper(),
            "stop_command_hex": STOP_STREAM.hex(" ").upper(),
            "raw_force_units": "Kg as written in Kunwei manual",
            "raw_moment_units": "Kg*m as written in Kunwei manual",
            "configuration_write_not_used": True,
        },
        "requested_duration_s": args.duration_s,
        "min_samples": args.min_samples,
        "summary_path": str(args.summary),
        "raw_frames_path": None if args.raw_frames is None else str(args.raw_frames),
        "stream_start_command_sent": False,
        "stream_stop_command_sent": False,
        "bytes_received": 0,
        "packets_received": 0,
        "dropped_sync_bytes": 0,
        "parse_errors": 0,
        "samples": 0,
    }


def _sample_summary(samples: list[tuple[float, ...]], sample_times: list[float]) -> dict[str, Any]:
    if not samples:
        return {
            "samples": 0,
            "sample_window_s": 0.0,
            "sample_rate_hz": None,
            "baseline_manual_units": None,
            "baseline_si_units": None,
        }
    width = float(len(samples))
    mean_values = [sum(sample[index] for sample in samples) / width for index in range(len(FIELDS))]
    sample_window = sample_times[-1] - sample_times[0] if len(sample_times) > 1 else 0.0
    sample_rate = (len(samples) - 1) / sample_window if sample_window > 0.0 and len(samples) > 1 else None
    force_n = [value * FORCE_KG_TO_N for value in mean_values[:3]]
    moment_nm = [value * MOMENT_KG_M_TO_NM for value in mean_values[3:]]
    return {
        "samples": len(samples),
        "sample_window_s": sample_window,
        "sample_rate_hz": sample_rate,
        "baseline_manual_units": dict(zip(FIELDS, mean_values)),
        "baseline_si_units": {
            "Fx_N": force_n[0],
            "Fy_N": force_n[1],
            "Fz_N": force_n[2],
            "Mx_Nm": moment_nm[0],
            "My_Nm": moment_nm[1],
            "Mz_Nm": moment_nm[2],
            "force_norm_N": math.sqrt(sum(value * value for value in force_n)),
        },
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
