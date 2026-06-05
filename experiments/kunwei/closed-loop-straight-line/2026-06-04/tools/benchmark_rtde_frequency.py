#!/usr/bin/env python3
"""No-motion Kunwei -> UR RTDE input-register frequency benchmark.

Live-use boundary:
- sends Kunwei 0x48 stream command only with --allow-kunwei-stream-command
- writes UR RTDE input registers during the benchmark
- subscribes to UR RTDE output registers that a teach-pendant echo script mirrors
- does not upload URScript, start a UR program, move the robot, write TCP/payload,
  call zero_ftsensor(), or send Kunwei zero/tare/config writes
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import select
import socket
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = EXPERIMENT_ROOT / "tools"
KUNWEI_TOOLS = Path("/home/andy/ur10e_ros2_ws/ft_sensor/kunwei/kwr75b/tools")
UR_REALSETUP_SCRIPTS = Path("/home/andy/codex-private-skills/skills/ur10e-realsetup/scripts")
sys.path.insert(0, str(TOOLS_DIR))
sys.path.insert(0, str(KUNWEI_TOOLS))
sys.path.insert(0, str(UR_REALSETUP_SCRIPTS))

from capture_kunwei_kwr75_1khz import (  # noqa: E402
    FORCE_KG_TO_N,
    MOMENT_KG_M_TO_NM,
    START_STREAM,
    STOP_STREAM,
    parse_frame,
    pop_frames,
)
from _ur_common import dashboard_exchange  # noqa: E402
from kunwei_rtde_bridge import (  # noqa: E402
    INPUT_FIELDS,
    INPUT_NAMES,
    OUTPUT_FIELDS,
    RTDEBridgeClient,
    flatten_output,
    guard_stop_reason,
    normal_component,
    vec_norm,
    write_json,
)


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def default_output_dir() -> Path:
    return EXPERIMENT_ROOT / "runs" / f"rtde_frequency_benchmark_{now_stamp()}"


def parse_rates(value: str) -> list[float]:
    rates: list[float] = []
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        rate = float(token)
        if rate <= 0:
            raise argparse.ArgumentTypeError("rates must be positive")
        rates.append(rate)
    if not rates:
        raise argparse.ArgumentTypeError("at least one rate is required")
    return rates


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * pct
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[int(index)]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def interval_stats(times: list[float]) -> dict[str, Any]:
    if len(times) < 2:
        return {"samples": len(times), "rate_hz": 0.0}
    intervals = [b - a for a, b in zip(times, times[1:]) if b > a]
    elapsed = times[-1] - times[0]
    return {
        "samples": len(times),
        "elapsed_s": elapsed,
        "rate_hz": (len(times) - 1) / elapsed if elapsed > 0 else 0.0,
        "dt_mean_s": statistics.fmean(intervals) if intervals else None,
        "dt_p95_s": percentile(intervals, 0.95),
        "dt_p99_s": percentile(intervals, 0.99),
        "dt_max_s": max(intervals) if intervals else None,
    }


def fmt_hz(rate: float) -> str:
    if float(rate).is_integer():
        return str(int(rate))
    return str(rate).replace(".", "p")


def row_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.9g}"
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--robot-host", default="192.168.1.18")
    parser.add_argument("--sensor-ip", default="192.168.50.25")
    parser.add_argument("--sensor-port", type=int, default=5152)
    parser.add_argument("--rates", type=parse_rates, default=parse_rates("125,250,500,1000"))
    parser.add_argument("--duration-s", type=float, default=8.0)
    parser.add_argument("--baseline-s", type=float, default=3.0)
    parser.add_argument("--target-force-n", type=float, default=3.0)
    parser.add_argument("--normal-axis", choices=("fx", "fy", "fz"), default="fz")
    parser.add_argument("--normal-sign", type=float, choices=(-1.0, 1.0), default=1.0)
    parser.add_argument("--output-dir", type=Path, default=default_output_dir())
    parser.add_argument("--connect-timeout-s", type=float, default=3.0)
    parser.add_argument("--socket-timeout-s", type=float, default=0.0)
    parser.add_argument("--allow-kunwei-stream-command", action="store_true")
    parser.add_argument("--no-start-command", action="store_true")
    parser.add_argument("--no-stop-command", action="store_true")
    parser.add_argument("--skip-dashboard-preflight", action="store_true")
    parser.add_argument("--max-normal-force-n", type=float, default=12.0)
    parser.add_argument("--max-force-norm-n", type=float, default=15.0)
    parser.add_argument("--max-torque-norm-nm", type=float, default=0.6)
    parser.add_argument("--sensor-stale-s", type=float, default=0.08)
    parser.add_argument("--strict-1khz-min-echo-rate-hz", type=float, default=950.0)
    parser.add_argument("--strict-1khz-max-echo-gap-s", type=float, default=0.003)
    parser.add_argument("--rate-pass-fraction", type=float, default=0.9)
    parser.add_argument("--between-rate-pause-s", type=float, default=0.5)
    return parser.parse_args(argv)


def drain_sensor(
    sock: socket.socket,
    buffer: bytearray,
    raw_handle: Any,
    baseline_samples: list[list[float]],
    baseline: list[float],
    args: argparse.Namespace,
    raw_prefix: int | None,
) -> tuple[int, int, int, float | None, list[float]]:
    frames_total = 0
    parse_errors = 0
    dropped_total = 0
    latest_frame_time: float | None = None
    latest_zeroed: list[float] = [0.0] * 6
    while True:
        ready, _, _ = select.select([sock], [], [], 0.0)
        if not ready:
            break
        try:
            chunk = sock.recv(8192)
        except BlockingIOError:
            break
        except socket.timeout:
            break
        if not chunk:
            break
        buffer.extend(chunk)
        frames, dropped = pop_frames(buffer, raw_prefix)
        dropped_total += dropped
        for frame in frames:
            try:
                raw_values = parse_frame(frame)
            except ValueError:
                parse_errors += 1
                continue
            raw_handle.write(frame)
            frames_total += 1
            latest_frame_time = time.monotonic()
            raw_si = [
                raw_values[0] * FORCE_KG_TO_N,
                raw_values[1] * FORCE_KG_TO_N,
                raw_values[2] * FORCE_KG_TO_N,
                raw_values[3] * MOMENT_KG_M_TO_NM,
                raw_values[4] * MOMENT_KG_M_TO_NM,
                raw_values[5] * MOMENT_KG_M_TO_NM,
            ]
            if baseline_samples is not None:
                baseline_samples.append(raw_si)
            latest_zeroed = [value - offset for value, offset in zip(raw_si, baseline)]
    return frames_total, parse_errors, dropped_total, latest_frame_time, latest_zeroed


def collect_baseline(
    sock: socket.socket,
    raw_handle: Any,
    buffer: bytearray,
    args: argparse.Namespace,
    raw_prefix: int | None,
) -> tuple[list[float], dict[str, Any], int, int, int]:
    start = time.monotonic()
    baseline_samples: list[list[float]] = []
    sensor_times: list[float] = []
    parse_errors = 0
    dropped = 0
    frames = 0
    while time.monotonic() - start < args.baseline_s:
        got, errors, dropped_now, frame_time, _latest = drain_sensor(
            sock,
            buffer,
            raw_handle,
            baseline_samples,
            [0.0] * 6,
            args,
            raw_prefix,
        )
        frames += got
        parse_errors += errors
        dropped += dropped_now
        if frame_time is not None:
            sensor_times.extend([frame_time] * got)
        time.sleep(0.0002)
    if not baseline_samples:
        raise RuntimeError("no Kunwei frames collected during baseline")
    baseline = [statistics.fmean(axis) for axis in zip(*baseline_samples)]
    summary = {
        "duration_s": time.monotonic() - start,
        "samples": len(baseline_samples),
        "sample_rate_hz_by_count": len(baseline_samples) / max(time.monotonic() - start, 1e-9),
        "offsets_si": dict(zip(["fx_n", "fy_n", "fz_n", "mx_nm", "my_nm", "mz_nm"], baseline)),
    }
    return baseline, summary, frames, parse_errors, dropped


def benchmark_one_rate(
    rate_hz: float,
    args: argparse.Namespace,
    sensor_sock: socket.socket,
    sensor_buffer: bytearray,
    raw_handle: Any,
    baseline: list[float],
    raw_prefix: int | None,
) -> dict[str, Any]:
    rate_label = fmt_hz(rate_hz)
    csv_path = args.output_dir / f"rtde_echo_events_{rate_label}hz.csv"
    fields = [
        "event",
        "index",
        "t_wall_ns",
        "t_monotonic_s",
        "requested_hz",
        "heartbeat_sent",
        "sensor_age_s",
        "sensor_ok",
        "normal_force_n",
        "force_norm_n",
        "torque_norm_nm",
        "guard_reason",
        "ur_runtime_state",
        "ur_robot_mode",
        "ur_safety_mode",
        "ur_speed_scaling",
        "ur_output_double_register_24",
        "ur_output_double_register_25",
        "ur_output_double_register_26",
        "ur_output_double_register_27",
        "ur_output_double_register_28",
        "ur_output_double_register_29",
        "ur_output_double_register_30",
        "ur_output_double_register_31",
        "ur_output_double_register_32",
        "ur_output_double_register_33",
        "ur_output_double_register_34",
        "ur_output_double_register_35",
    ]

    latest_zeroed = [0.0] * 6
    latest_frame_time: float | None = None
    send_times: list[float] = []
    output_times: list[float] = []
    echo_transition_times: list[float] = []
    sensor_times: list[float] = []
    echo_values: list[float] = []
    parse_errors = 0
    dropped_sync_bytes = 0
    sensor_frames = 0
    heartbeat = 0.0
    missed_write_slots = 0
    guard_reason: str | None = None
    stop_reason = "duration"

    try:
        with RTDEBridgeClient(args.robot_host, timeout=args.connect_timeout_s) as rtde:
            rtde.negotiate()
            output_recipe, output_types = rtde.setup_outputs(rate_hz, OUTPUT_FIELDS)
            input_recipe, input_types = rtde.setup_inputs(INPUT_FIELDS)
            rtde.start()

            start = time.monotonic()
            deadline = start + args.duration_s
            next_send = start
            period = 1.0 / rate_hz
            last_echo_heartbeat: float | None = None
            send_index = 0
            output_index = 0

            with csv_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                while time.monotonic() < deadline:
                    now = time.monotonic()
                    got, errors, dropped, frame_time, zeroed = drain_sensor(
                        sensor_sock,
                        sensor_buffer,
                        raw_handle,
                        None,
                        baseline,
                        args,
                        raw_prefix,
                    )
                    sensor_frames += got
                    parse_errors += errors
                    dropped_sync_bytes += dropped
                    if got:
                        latest_frame_time = frame_time
                        latest_zeroed = zeroed
                        sensor_times.extend([time.monotonic()] * got)

                    while True:
                        try:
                            sample = rtde.recv_available_sample(output_recipe, output_types, timeout_s=0.0)
                        except (BlockingIOError, socket.timeout):
                            break
                        if sample is None:
                            break
                        output_index += 1
                        output_time = time.monotonic()
                        output_times.append(output_time)
                        echo = sample.get("output_double_register_26")
                        if echo is not None:
                            echo_float = float(echo)
                            echo_values.append(echo_float)
                            if last_echo_heartbeat is None or echo_float != last_echo_heartbeat:
                                echo_transition_times.append(output_time)
                                last_echo_heartbeat = echo_float
                        row = {
                            "event": "rtde_output",
                            "index": output_index,
                            "t_wall_ns": time.time_ns(),
                            "t_monotonic_s": output_time,
                            "requested_hz": rate_hz,
                            "heartbeat_sent": "",
                            "sensor_age_s": "",
                            "sensor_ok": "",
                            "normal_force_n": "",
                            "force_norm_n": "",
                            "torque_norm_nm": "",
                            "guard_reason": "",
                        }
                        row.update({key: row_value(value) for key, value in flatten_output(sample).items()})
                        writer.writerow({field: row_value(row.get(field)) for field in fields})

                    now = time.monotonic()
                    if now >= next_send:
                        while next_send + period < now:
                            missed_write_slots += 1
                            next_send += period
                        sensor_age = math.inf if latest_frame_time is None else now - latest_frame_time
                        sensor_ok = 1.0 if sensor_age <= args.sensor_stale_s and parse_errors == 0 else 0.0
                        values = {
                            "normal_force_n": normal_component(latest_zeroed, args.normal_axis, args.normal_sign),
                            "force_norm_n": vec_norm(latest_zeroed[:3]),
                            "heartbeat": heartbeat,
                            "sensor_ok": sensor_ok,
                            "stop_request": 0.0,
                            "target_force_n": args.target_force_n,
                            "torque_norm_nm": vec_norm(latest_zeroed[3:]),
                            "fx_n_zeroed": latest_zeroed[0],
                            "fy_n_zeroed": latest_zeroed[1],
                            "fz_n_zeroed": latest_zeroed[2],
                            "mx_nm_zeroed": latest_zeroed[3],
                            "my_nm_zeroed": latest_zeroed[4],
                            "mz_nm_zeroed": latest_zeroed[5],
                        }
                        if sensor_ok:
                            guard_reason = guard_stop_reason(args, values)
                            if guard_reason is not None:
                                values["stop_request"] = 1.0
                                stop_reason = guard_reason
                        rtde.send_input_sample(input_recipe, input_types, [values[name] for name in INPUT_NAMES])
                        send_index += 1
                        send_times.append(now)
                        writer.writerow(
                            {
                                "event": "rtde_send",
                                "index": send_index,
                                "t_wall_ns": time.time_ns(),
                                "t_monotonic_s": row_value(now),
                                "requested_hz": row_value(rate_hz),
                                "heartbeat_sent": row_value(heartbeat),
                                "sensor_age_s": "" if not math.isfinite(sensor_age) else row_value(sensor_age),
                                "sensor_ok": row_value(sensor_ok),
                                "normal_force_n": row_value(values["normal_force_n"]),
                                "force_norm_n": row_value(values["force_norm_n"]),
                                "torque_norm_nm": row_value(values["torque_norm_nm"]),
                                "guard_reason": guard_reason or "",
                            }
                        )
                        heartbeat += 1.0
                        next_send += period
                        if guard_reason is not None:
                            break
                    time.sleep(0.0001)
                if guard_reason is not None:
                    stop_reason = guard_reason
    except Exception as exc:
        return {
            "requested_hz": rate_hz,
            "status": "setup_or_runtime_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "event_csv": str(csv_path),
        }

    send_stats = interval_stats(send_times)
    output_stats = interval_stats(output_times)
    echo_stats = interval_stats(echo_transition_times)
    sensor_stats = interval_stats(sensor_times)
    rate_pass = (
        echo_stats["rate_hz"] >= args.rate_pass_fraction * rate_hz
        and (echo_stats.get("dt_max_s") is None or echo_stats["dt_max_s"] <= 3.0 / rate_hz)
    )
    strict_1khz_pass = False
    if rate_hz >= 1000.0:
        strict_1khz_pass = (
            echo_stats["rate_hz"] >= args.strict_1khz_min_echo_rate_hz
            and (echo_stats.get("dt_max_s") is not None and echo_stats["dt_max_s"] <= args.strict_1khz_max_echo_gap_s)
        )

    return {
        "requested_hz": rate_hz,
        "status": stop_reason,
        "event_csv": str(csv_path),
        "sensor_frames": sensor_frames,
        "parse_errors": parse_errors,
        "dropped_sync_bytes": dropped_sync_bytes,
        "missed_write_slots": missed_write_slots,
        "rtde_send": send_stats,
        "rtde_output": output_stats,
        "echo_heartbeat_transitions": echo_stats,
        "kunwei_sensor_during_rate": sensor_stats,
        "last_echo_heartbeat": echo_values[-1] if echo_values else None,
        "rate_pass": rate_pass,
        "strict_1khz_pass": strict_1khz_pass,
        "pass_rule": {
            "rate_pass": f"echo_transition_rate >= {args.rate_pass_fraction} * requested_hz and max echo gap <= 3/requested_hz",
            "strict_1khz": f"for requested >=1000: echo_transition_rate >= {args.strict_1khz_min_echo_rate_hz} Hz and max echo gap <= {args.strict_1khz_max_echo_gap_s} s",
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.duration_s <= 0 or args.baseline_s <= 0:
        raise SystemExit("duration and baseline must be positive")
    if not args.no_start_command and not args.allow_kunwei_stream_command:
        raise SystemExit("Refusing to send Kunwei stream command without --allow-kunwei-stream-command")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = args.output_dir / "metadata.json"
    summary_path = args.output_dir / "summary.json"
    raw_path = args.output_dir / "raw_frames.bin"

    dashboard: dict[str, str] | None = None
    if not args.skip_dashboard_preflight:
        dashboard = dashboard_exchange(
            args.robot_host,
            ["is in remote control", "safetymode", "robotmode", "running", "programState"],
        )
        if "NORMAL" not in dashboard.get("safetymode", ""):
            raise SystemExit(f"Dashboard safety not NORMAL: {dashboard}")

    metadata = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "args": {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
        "dashboard_preflight": dashboard,
        "safety_boundary": [
            "no URScript upload or program start",
            "no robot motion command from Python",
            "no UR TCP/payload writes",
            "no zero_ftsensor",
            "no Kunwei zero/tare/config write",
        ],
        "operator_step": "Start programs/kunwei_register_echo.script on the teach pendant while this benchmark is running.",
        "register_map": dict(zip(INPUT_FIELDS, INPUT_NAMES)),
    }
    write_json(metadata_path, metadata)

    sock: socket.socket | None = None
    buffer = bytearray()
    parse_errors = 0
    dropped_sync_bytes = 0
    raw_prefix = 0x48 if not args.no_start_command else None
    try:
        sock = socket.create_connection((args.sensor_ip, args.sensor_port), timeout=args.connect_timeout_s)
        sock.settimeout(args.socket_timeout_s)
        if not args.no_start_command:
            sock.sendall(START_STREAM)

        with raw_path.open("wb") as raw_handle:
            baseline, baseline_summary, baseline_frames, baseline_parse_errors, baseline_dropped = collect_baseline(
                sock,
                raw_handle,
                buffer,
                args,
                raw_prefix,
            )
            parse_errors += baseline_parse_errors
            dropped_sync_bytes += baseline_dropped

            rate_summaries = []
            for rate_hz in args.rates:
                rate_summaries.append(
                    benchmark_one_rate(rate_hz, args, sock, buffer, raw_handle, baseline, raw_prefix)
                )
                time.sleep(args.between_rate_pause_s)
    finally:
        if sock is not None and not args.no_stop_command:
            try:
                sock.sendall(STOP_STREAM)
            except OSError:
                pass
        if sock is not None:
            sock.close()

    passed_rates = [
        item["requested_hz"]
        for item in rate_summaries
        if item.get("status") == "duration" and item.get("rate_pass")
    ]
    strict_1khz_pass = any(item.get("strict_1khz_pass") for item in rate_summaries)
    total_parse_errors = parse_errors + sum(int(item.get("parse_errors", 0)) for item in rate_summaries)
    total_dropped_sync_bytes = dropped_sync_bytes + sum(
        int(item.get("dropped_sync_bytes", 0)) for item in rate_summaries
    )
    summary = {
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "baseline": baseline_summary,
        "baseline_frames": baseline_frames,
        "baseline_parse_errors": parse_errors,
        "total_parse_errors": total_parse_errors,
        "dropped_sync_bytes": total_dropped_sync_bytes,
        "rates": rate_summaries,
        "max_stable_requested_hz": max(passed_rates) if passed_rates else None,
        "strict_1khz_pass": strict_1khz_pass,
        "interpretation": (
            "1 kHz robot-side register echo passed by the strict rule."
            if strict_1khz_pass
            else "1 kHz robot-side register echo not proven; use the max stable requested rate above."
        ),
        "paths": {
            "metadata": str(metadata_path),
            "summary": str(summary_path),
            "raw_frames": str(raw_path),
        },
    }
    write_json(summary_path, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if total_parse_errors == 0 and any(item.get("status") == "duration" for item in rate_summaries) else 3


if __name__ == "__main__":
    raise SystemExit(main())
