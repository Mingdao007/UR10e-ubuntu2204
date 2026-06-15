#!/usr/bin/env python3
"""Open-permission OnRobot HEX high-speed UDP reference client.

This script is a separate, explicitly gated reference implementation for the
vendor/ros2_net_ft_driver style surface. It can send SPEED, FILTER, and BIAS
commands when live execution is deliberately enabled.

Do not use this script as a default probe. It exists so configuration-writing
behavior is isolated from the safer START/STOP-only probe.
"""

from __future__ import annotations

import argparse
import json
import socket
import time
from datetime import datetime
from pathlib import Path

from _onrobot_highspeed_udp import (
    BIASING_OFF,
    BIASING_ON,
    COMMAND_BIAS,
    COMMAND_FILTER,
    COMMAND_SPEED,
    COMMAND_START,
    COMMAND_STOP,
    UDP_PORT,
    command_name,
    make_udp_socket,
    receive_sample,
    sample_to_row,
    send_command,
    summarize_run,
    write_rows,
    write_summary,
)


CONFIRM_PHRASE = "OPEN_ONROBOT_UDP_CONFIG"
DEVICE_ROOT = Path(__file__).resolve().parents[1]


def default_output_dir() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return DEVICE_ROOT / "measurements" / f"udp_highspeed_open_reference_{stamp}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reference client that may send OnRobot UDP SPEED/FILTER/BIAS "
            "configuration commands. Defaults to dry-run."
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
        help="Data field for START. Defaults to --max-samples.",
    )
    parser.add_argument("--timeout-s", type=float, default=2.0)
    parser.add_argument("--command-delay-s", type=float, default=0.005)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--prefix", default="onrobot_udp_open_reference")
    parser.add_argument(
        "--profile",
        choices=["vendor-demo", "custom"],
        default="vendor-demo",
        help=(
            "vendor-demo mirrors highspeed_udp.c: SPEED 10, FILTER 4, BIAS off, "
            "then mid-run BIAS on. custom sends only explicitly requested config."
        ),
    )
    parser.add_argument(
        "--speed-divisor",
        type=int,
        default=None,
        help="Send SPEED / 0x0082 with this divisor. Vendor comment: 1000/divisor Hz.",
    )
    parser.add_argument(
        "--filter-mode",
        type=int,
        default=None,
        help="Send FILTER / 0x0081. Modes: 0 none, 1 500Hz, 2 150Hz, 3 50Hz, 4 15Hz, 5 5Hz, 6 1.5Hz.",
    )
    parser.add_argument(
        "--initial-bias",
        choices=["none", "off", "on"],
        default=None,
        help="Send BIAS / 0x0042 before START.",
    )
    parser.add_argument(
        "--midrun-bias",
        choices=["none", "off", "on"],
        default=None,
        help="Send BIAS / 0x0042 during receive loop.",
    )
    parser.add_argument(
        "--midrun-at-sample",
        type=int,
        default=None,
        help="Sample index before sending mid-run bias. Defaults to max_samples // 2.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually send UDP commands. Without this, only prints the plan.",
    )
    parser.add_argument(
        "--allow-config-writes",
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
    if args.speed_divisor is not None and args.speed_divisor <= 0:
        parser.error("--speed-divisor must be positive")
    if args.filter_mode is not None and not 0 <= args.filter_mode <= 6:
        parser.error("--filter-mode must be between 0 and 6")
    if args.midrun_at_sample is not None and args.midrun_at_sample < 0:
        parser.error("--midrun-at-sample must be non-negative")
    if args.live and args.allow_config_writes != CONFIRM_PHRASE:
        parser.error(f"--live requires --allow-config-writes {CONFIRM_PHRASE!r}")


def bias_data(value: str) -> int:
    if value == "on":
        return BIASING_ON
    if value == "off":
        return BIASING_OFF
    raise ValueError(f"unsupported bias value: {value}")


def resolve_config(args: argparse.Namespace) -> dict:
    if args.profile == "vendor-demo":
        speed_divisor = args.speed_divisor if args.speed_divisor is not None else 10
        filter_mode = args.filter_mode if args.filter_mode is not None else 4
        initial_bias = args.initial_bias if args.initial_bias is not None else "off"
        midrun_bias = args.midrun_bias if args.midrun_bias is not None else "on"
    else:
        speed_divisor = args.speed_divisor
        filter_mode = args.filter_mode
        initial_bias = args.initial_bias if args.initial_bias is not None else "none"
        midrun_bias = args.midrun_bias if args.midrun_bias is not None else "none"

    midrun_at_sample = (
        args.midrun_at_sample
        if args.midrun_at_sample is not None
        else max(0, args.max_samples // 2)
    )
    start_count = args.start_count if args.start_count is not None else args.max_samples
    return {
        "profile": args.profile,
        "speed_divisor": speed_divisor,
        "filter_mode": filter_mode,
        "initial_bias": initial_bias,
        "midrun_bias": midrun_bias,
        "midrun_at_sample": midrun_at_sample,
        "start_count": start_count,
    }


def pre_start_commands(config: dict) -> list[tuple[int, int]]:
    commands: list[tuple[int, int]] = []
    if config["speed_divisor"] is not None:
        commands.append((COMMAND_SPEED, int(config["speed_divisor"])))
    if config["filter_mode"] is not None:
        commands.append((COMMAND_FILTER, int(config["filter_mode"])))
    if config["initial_bias"] != "none":
        commands.append((COMMAND_BIAS, bias_data(config["initial_bias"])))
    return commands


def plan_dict(args: argparse.Namespace, config: dict) -> dict:
    commands = [
        {
            "command": command_name(command),
            "command_hex": f"0x{command:04x}",
            "data": data,
        }
        for command, data in pre_start_commands(config)
    ]
    commands.append(
        {"command": "START", "command_hex": "0x0002", "data": config["start_count"]}
    )
    if config["midrun_bias"] != "none":
        commands.append(
            {
                "command": "BIAS",
                "command_hex": "0x0042",
                "data": bias_data(config["midrun_bias"]),
                "when": f"before receiving sample index {config['midrun_at_sample']}",
            }
        )
    commands.append({"command": "STOP", "command_hex": "0x0000", "data": 0, "when": "finally"})
    return {
        "mode": "live" if args.live else "dry-run",
        "host": args.host,
        "port": args.port,
        "seconds": args.seconds,
        "max_samples": args.max_samples,
        "output_dir": str(args.output_dir or default_output_dir()),
        "profile": config["profile"],
        "commands_that_would_be_sent": commands,
        "live_gate": f"--live --allow-config-writes {CONFIRM_PHRASE}",
        "warning": "This script can write OnRobot UDP speed/filter/bias settings.",
    }


def collect(args: argparse.Namespace, config: dict) -> int:
    output_dir = args.output_dir or default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = output_dir / f"{args.prefix}_{stamp}.csv"
    summary_path = output_dir / f"{args.prefix}_{stamp}_summary.json"

    rows: list[dict] = []
    samples = []
    elapsed_s: list[float] = []
    recv_latencies_ms: list[float] = []
    errors: list[str] = []
    commands_sent: list[dict] = []
    started_wall = datetime.now().isoformat(timespec="seconds")
    start_ns = time.perf_counter_ns()
    started_stream = False
    midrun_sent = False

    try:
        with make_udp_socket(args.host, args.port, args.timeout_s) as sock:
            for command, data in pre_start_commands(config):
                commands_sent.append(send_command(sock, command, data, args.command_delay_s))
            commands_sent.append(
                send_command(sock, COMMAND_START, config["start_count"], args.command_delay_s)
            )
            started_stream = True
            while len(samples) < args.max_samples:
                if (
                    config["midrun_bias"] != "none"
                    and not midrun_sent
                    and len(samples) >= config["midrun_at_sample"]
                ):
                    commands_sent.append(
                        send_command(
                            sock,
                            COMMAND_BIAS,
                            bias_data(config["midrun_bias"]),
                            args.command_delay_s,
                        )
                    )
                    midrun_sent = True

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
            "open-permission reference client",
            "may send SPEED/FILTER/BIAS when requested or when profile=vendor-demo",
            "no TCP/payload/URScript/URCap/robot-motion commands",
            "final STOP attempted after START",
        ],
        extra={
            "configuration": config,
            "script_mode": "open_reference_allows_speed_filter_bias",
        },
    )
    write_summary(summary_path, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not errors else 2


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    validate_args(parser, args)
    config = resolve_config(args)
    if not args.live:
        print(json.dumps(plan_dict(args, config), indent=2, sort_keys=True))
        return 0
    return collect(args, config)


if __name__ == "__main__":
    raise SystemExit(main())
