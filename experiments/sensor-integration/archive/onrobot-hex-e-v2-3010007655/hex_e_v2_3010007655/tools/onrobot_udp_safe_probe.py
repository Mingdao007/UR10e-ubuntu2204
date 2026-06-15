#!/usr/bin/env python3
"""Safer OnRobot HEX high-speed UDP probe.

This script is intentionally narrower than the vendor sample:

- sends UDP START and final STOP only when --live is explicitly confirmed
- never sends BIAS, FILTER, or SPEED commands
- never sends zero, TCP/payload, URScript, URCap, force-control, or robot-motion
  commands

It is still not passive read-only. START/STOP changes the Compute Box streaming
state, so live use requires an explicit operator approval step.
"""

from __future__ import annotations

import argparse
import json
import socket
import time
from datetime import datetime
from pathlib import Path

from _onrobot_highspeed_udp import (
    COMMAND_START,
    COMMAND_STOP,
    UDP_PORT,
    make_udp_socket,
    receive_sample,
    sample_to_row,
    send_command,
    summarize_run,
    write_rows,
    write_summary,
)


CONFIRM_PHRASE = "START_STOP_ONLY"
DEVICE_ROOT = Path(__file__).resolve().parents[1]


def default_output_dir() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return DEVICE_ROOT / "measurements" / f"udp_highspeed_safe_probe_{stamp}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Probe OnRobot high-speed UDP without sending bias/filter/speed "
            "commands. Defaults to dry-run."
        )
    )
    parser.add_argument("--host", default="192.168.1.1")
    parser.add_argument("--port", type=int, default=UDP_PORT)
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--max-samples", type=int, default=2500)
    parser.add_argument(
        "--start-count",
        type=int,
        default=None,
        help="Data field for START. Defaults to --max-samples to keep streaming bounded.",
    )
    parser.add_argument("--timeout-s", type=float, default=2.0)
    parser.add_argument("--command-delay-s", type=float, default=0.005)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--prefix", default="onrobot_udp_safe_probe")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually send START/STOP UDP commands. Without this, only prints the plan.",
    )
    parser.add_argument(
        "--confirm-start-stop",
        default="",
        help=f"Required with --live. Exact value: {CONFIRM_PHRASE}",
    )
    return parser


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.seconds <= 0:
        parser.error("--seconds must be positive")
    if args.max_samples <= 0:
        parser.error("--max-samples must be positive")
    if args.timeout_s <= 0:
        parser.error("--timeout-s must be positive")
    if args.command_delay_s < 0:
        parser.error("--command-delay-s must be non-negative")
    start_count = args.start_count if args.start_count is not None else args.max_samples
    if start_count <= 0:
        parser.error("--start-count must be positive when provided")
    if args.live and args.confirm_start_stop != CONFIRM_PHRASE:
        parser.error(f"--live requires --confirm-start-stop {CONFIRM_PHRASE!r}")


def plan_dict(args: argparse.Namespace) -> dict:
    start_count = args.start_count if args.start_count is not None else args.max_samples
    return {
        "mode": "live" if args.live else "dry-run",
        "host": args.host,
        "port": args.port,
        "seconds": args.seconds,
        "max_samples": args.max_samples,
        "output_dir": str(args.output_dir or default_output_dir()),
        "commands_that_would_be_sent": [
            {"command": "START", "command_hex": "0x0002", "data": start_count},
            {"command": "STOP", "command_hex": "0x0000", "data": 0, "when": "finally"},
        ],
        "forbidden_by_this_script": [
            "BIAS / 0x0042",
            "FILTER / 0x0081",
            "SPEED / 0x0082",
            "zero/TCP/payload/URScript/URCap/robot-motion commands",
        ],
        "live_gate": f"--live --confirm-start-stop {CONFIRM_PHRASE}",
    }


def collect(args: argparse.Namespace) -> int:
    output_dir = args.output_dir or default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = output_dir / f"{args.prefix}_{stamp}.csv"
    summary_path = output_dir / f"{args.prefix}_{stamp}_summary.json"

    start_count = args.start_count if args.start_count is not None else args.max_samples
    rows: list[dict] = []
    samples = []
    elapsed_s: list[float] = []
    recv_latencies_ms: list[float] = []
    errors: list[str] = []
    commands_sent: list[dict] = []
    started_wall = datetime.now().isoformat(timespec="seconds")
    start_ns = time.perf_counter_ns()
    started_stream = False

    try:
        with make_udp_socket(args.host, args.port, args.timeout_s) as sock:
            commands_sent.append(
                send_command(sock, COMMAND_START, start_count, delay_s=args.command_delay_s)
            )
            started_stream = True
            while len(samples) < args.max_samples:
                before_ns = time.perf_counter_ns()
                if (before_ns - start_ns) / 1e9 >= args.seconds:
                    break
                try:
                    sample = receive_sample(sock)
                except socket.timeout as exc:
                    errors.append(f"socket.timeout: {exc}")
                    break
                except Exception as exc:  # noqa: BLE001 - preserve hardware/protocol error text
                    errors.append(f"{type(exc).__name__}: {exc}")
                    break
                after_ns = time.perf_counter_ns()
                t_s = (after_ns - start_ns) / 1e9
                latency_ms = (after_ns - before_ns) / 1e6
                samples.append(sample)
                elapsed_s.append(t_s)
                recv_latencies_ms.append(latency_ms)
                rows.append(sample_to_row(len(samples) - 1, t_s, latency_ms, sample))
            if started_stream:
                commands_sent.append(
                    send_command(sock, COMMAND_STOP, 0, delay_s=args.command_delay_s)
                )
                started_stream = False
    finally:
        if started_stream:
            try:
                with make_udp_socket(args.host, args.port, args.timeout_s) as stop_sock:
                    commands_sent.append(
                        send_command(stop_sock, COMMAND_STOP, 0, delay_s=args.command_delay_s)
                    )
            except Exception as exc:  # noqa: BLE001 - include cleanup failure in summary
                errors.append(f"final STOP failed: {type(exc).__name__}: {exc}")

    write_rows(csv_path, rows)
    summary = summarize_run(
        samples=samples,
        elapsed_s=elapsed_s,
        recv_latencies_ms=recv_latencies_ms,
        errors=errors,
        commands_sent=commands_sent,
        csv_path=csv_path,
        summary_path=summary_path,
        host=args.host,
        port=args.port,
        requested_seconds=args.seconds,
        max_samples=args.max_samples,
        started_wall=started_wall,
        finished_wall=datetime.now().isoformat(timespec="seconds"),
        safety_boundary=[
            "START and final STOP only",
            "no BIAS command",
            "no FILTER command",
            "no SPEED command",
            "no zero/TCP/payload/URScript/URCap/robot-motion commands",
        ],
        extra={
            "start_count": start_count,
            "script_mode": "safe_probe_no_bias_no_filter_no_speed",
        },
    )
    write_summary(summary_path, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not errors else 2


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    validate_args(parser, args)
    if not args.live:
        print(json.dumps(plan_dict(args), indent=2, sort_keys=True))
        return 0
    return collect(args)


if __name__ == "__main__":
    raise SystemExit(main())
