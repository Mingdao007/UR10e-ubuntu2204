#!/usr/bin/env python3
"""Kunwei KWR75B TCP to UR RTDE input-register bridge.

Live-use boundary:
- sends Kunwei 0x48 stream command only with --allow-kunwei-stream-command
- writes UR RTDE input registers only with --write-rtde-inputs
- does not send URScript, start a UR program, move the robot, write TCP/payload,
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
import struct
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
KUNWEI_TOOLS = Path("/home/andy/ur10e_ros2_ws/ft_sensor/kunwei/kwr75b/tools")
UR_REALSETUP_SCRIPTS = Path("/home/andy/codex-private-skills/skills/ur10e-realsetup/scripts")
sys.path.insert(0, str(KUNWEI_TOOLS))
sys.path.insert(0, str(UR_REALSETUP_SCRIPTS))

from capture_kunwei_kwr75_1khz import (  # noqa: E402
    FIELDS as KUNWEI_RAW_FIELDS,
    FORCE_KG_TO_N,
    MOMENT_KG_M_TO_NM,
    START_STREAM,
    STOP_STREAM,
    parse_frame,
    pop_frames,
)
from _ur_common import RTDEClient, dashboard_exchange  # noqa: E402


INPUT_FIELDS = [
    "input_double_register_24",
    "input_double_register_25",
    "input_double_register_26",
    "input_double_register_27",
    "input_double_register_28",
    "input_double_register_29",
    "input_double_register_30",
    "input_double_register_31",
    "input_double_register_32",
    "input_double_register_33",
    "input_double_register_34",
    "input_double_register_35",
    "input_double_register_36",
]
INPUT_NAMES = [
    "normal_force_n",
    "force_norm_n",
    "heartbeat",
    "sensor_ok",
    "stop_request",
    "target_force_n",
    "torque_norm_nm",
    "fx_n_zeroed",
    "fy_n_zeroed",
    "fz_n_zeroed",
    "mx_nm_zeroed",
    "my_nm_zeroed",
    "mz_nm_zeroed",
]
OUTPUT_FIELDS = [
    "actual_TCP_pose",
    "actual_TCP_speed",
    "runtime_state",
    "robot_mode",
    "safety_mode",
    "speed_scaling",
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
    "output_double_register_34",
    "output_double_register_35",
]


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def default_output_dir() -> Path:
    return EXPERIMENT_ROOT / "runs" / f"bridge_{now_stamp()}"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def vec_norm(values: list[float]) -> float:
    return math.sqrt(sum(value * value for value in values))


def stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"samples": 0}
    return {
        "samples": len(values),
        "mean": statistics.fmean(values),
        "std": statistics.stdev(values) if len(values) >= 2 else 0.0,
        "min": min(values),
        "max": max(values),
    }


class RTDEBridgeClient(RTDEClient):
    def setup_inputs(self, fields: list[str]) -> tuple[int, list[str]]:
        self._send_packet("I", ",".join(fields).encode())
        ptype, data = self._recv_packet()
        if ptype != ord("I"):
            raise RuntimeError(f"unexpected RTDE input setup response type {ptype}")
        recipe_id = data[0]
        type_names = data[1:].decode("ascii", errors="replace").split(",")
        if recipe_id == 0 or any(name == "NOT_FOUND" for name in type_names):
            raise RuntimeError(f"invalid RTDE input recipe: id={recipe_id} types={type_names}")
        return recipe_id, type_names

    def send_input_sample(self, recipe_id: int, type_names: list[str], values: list[Any]) -> None:
        if len(type_names) != len(values):
            raise ValueError("RTDE input type/value length mismatch")
        payload = bytearray([recipe_id])
        for type_name, value in zip(type_names, values):
            payload.extend(pack_rtde_value(type_name, value))
        self._send_packet("U", bytes(payload))

    def recv_available_sample(
        self, recipe_id: int, type_names: list[str], timeout_s: float = 0.0
    ) -> dict[str, Any] | None:
        assert self.sock is not None
        ready, _, _ = select.select([self.sock], [], [], timeout_s)
        if not ready:
            return None
        ptype, payload = self._recv_packet()
        if ptype != ord("U") or not payload or payload[0] != recipe_id:
            return None
        cursor = 1
        values: list[Any] = []
        for type_name in type_names:
            fmt = rtde_struct_format(type_name)
            width = struct.calcsize("!" + fmt)
            unpacked = struct.unpack("!" + fmt, payload[cursor : cursor + width])
            cursor += width
            values.append(unpacked[0] if len(unpacked) == 1 else list(unpacked))
        return {field: value for field, value in zip(OUTPUT_FIELDS, values)}


def rtde_struct_format(type_name: str) -> str:
    mapping = {
        "DOUBLE": "d",
        "VECTOR3D": "3d",
        "VECTOR6D": "6d",
        "UINT32": "I",
        "UINT64": "Q",
        "INT32": "i",
        "BOOL": "?",
    }
    if type_name not in mapping:
        raise RuntimeError(f"unsupported RTDE type: {type_name}")
    return mapping[type_name]


def pack_rtde_value(type_name: str, value: Any) -> bytes:
    fmt = rtde_struct_format(type_name)
    if fmt.endswith("d") and fmt != "d":
        return struct.pack("!" + fmt, *value)
    return struct.pack("!" + fmt, value)


def zeroed_si(raw_values: tuple[float, ...], baseline: list[float]) -> list[float]:
    si = [
        raw_values[0] * FORCE_KG_TO_N,
        raw_values[1] * FORCE_KG_TO_N,
        raw_values[2] * FORCE_KG_TO_N,
        raw_values[3] * MOMENT_KG_M_TO_NM,
        raw_values[4] * MOMENT_KG_M_TO_NM,
        raw_values[5] * MOMENT_KG_M_TO_NM,
    ]
    return [value - offset for value, offset in zip(si, baseline)]


def normal_component(values_si_zeroed: list[float], axis: str, sign: float) -> float:
    index = {"fx": 0, "fy": 1, "fz": 2}[axis]
    return sign * values_si_zeroed[index]


def flatten_output(output: dict[str, Any] | None) -> dict[str, Any]:
    row: dict[str, Any] = {}
    if not output:
        return row
    for key, value in output.items():
        if isinstance(value, list):
            for idx, item in enumerate(value):
                row[f"ur_{key}_{idx}"] = item
        else:
            row[f"ur_{key}"] = value
    return row


def guard_stop_reason(args: argparse.Namespace, bridge_values: dict[str, float]) -> str | None:
    if abs(bridge_values["normal_force_n"]) > args.max_normal_force_n:
        return "normal_force_guard"
    if bridge_values["force_norm_n"] > args.max_force_norm_n:
        return "force_norm_guard"
    if bridge_values["torque_norm_nm"] > args.max_torque_norm_nm:
        return "torque_norm_guard"
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--robot-host", default="192.168.1.18")
    parser.add_argument("--sensor-ip", default="192.168.50.25")
    parser.add_argument("--sensor-port", type=int, default=5152)
    parser.add_argument("--duration-s", type=float, default=30.0)
    parser.add_argument("--baseline-s", type=float, default=5.0)
    parser.add_argument("--rtde-hz", type=float, default=125.0)
    parser.add_argument("--target-force-n", type=float, default=3.0)
    parser.add_argument("--normal-axis", choices=("fx", "fy", "fz"), default="fz")
    parser.add_argument("--normal-sign", type=float, choices=(-1.0, 1.0), default=1.0)
    parser.add_argument("--output-dir", type=Path, default=default_output_dir())
    parser.add_argument("--connect-timeout-s", type=float, default=3.0)
    parser.add_argument("--socket-timeout-s", type=float, default=0.01)
    parser.add_argument("--allow-kunwei-stream-command", action="store_true")
    parser.add_argument("--no-start-command", action="store_true")
    parser.add_argument("--no-stop-command", action="store_true")
    parser.add_argument("--write-rtde-inputs", action="store_true")
    parser.add_argument("--skip-dashboard-preflight", action="store_true")
    parser.add_argument("--max-normal-force-n", type=float, default=12.0)
    parser.add_argument("--max-force-norm-n", type=float, default=15.0)
    parser.add_argument("--max-torque-norm-nm", type=float, default=0.6)
    parser.add_argument("--sensor-stale-s", type=float, default=0.08)
    parser.add_argument("--rezero-s", type=float, default=1.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.duration_s <= 0 or args.baseline_s < 0 or args.rtde_hz <= 0:
        raise SystemExit("duration, baseline, and RTDE rate must be positive")
    if not args.no_start_command and not args.allow_kunwei_stream_command:
        raise SystemExit("Refusing to send Kunwei stream command without --allow-kunwei-stream-command")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    sensor_csv_path = args.output_dir / "kunwei_sensor_1khz.csv"
    bridge_csv_path = args.output_dir / "bridge_rtde_125hz.csv"
    raw_path = args.output_dir / "raw_frames.bin"
    metadata_path = args.output_dir / "metadata.json"
    summary_path = args.output_dir / "summary.json"

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
        "register_map": dict(zip(INPUT_FIELDS, INPUT_NAMES)),
    }
    write_json(metadata_path, metadata)

    sock: socket.socket | None = None
    rtde: RTDEBridgeClient | None = None
    rtde_input_recipe = 0
    rtde_input_types: list[str] = []
    rtde_output_recipe = 0
    rtde_output_types: list[str] = []
    start_mono = time.monotonic()
    baseline_raw_si: list[list[float]] = []
    baseline = [0.0] * 6
    baseline_ready = args.baseline_s == 0
    baseline_start_mono = start_mono
    baseline_epoch = 0
    latest_zeroed = [0.0] * 6
    latest_frame_time: float | None = None
    latest_output: dict[str, Any] | None = None
    last_zero_request: float | None = None
    heartbeat = 0.0
    stop_request = 0.0
    stop_reason = "duration"
    guard_reason: str | None = None
    parse_errors = 0
    dropped_sync_bytes = 0
    samples = 0
    bridge_writes = 0
    normals: list[float] = []
    force_norms: list[float] = []
    torque_norms: list[float] = []
    zero_events: list[dict[str, Any]] = []
    buffer = bytearray()

    next_write = start_mono
    write_period = 1.0 / args.rtde_hz

    try:
        sock = socket.create_connection((args.sensor_ip, args.sensor_port), timeout=args.connect_timeout_s)
        sock.settimeout(args.socket_timeout_s)
        if not args.no_start_command:
            sock.sendall(START_STREAM)

        if args.write_rtde_inputs:
            rtde = RTDEBridgeClient(args.robot_host, timeout=args.connect_timeout_s)
            rtde.__enter__()
            rtde.negotiate()
            rtde_output_recipe, rtde_output_types = rtde.setup_outputs(args.rtde_hz, OUTPUT_FIELDS)
            rtde_input_recipe, rtde_input_types = rtde.setup_inputs(INPUT_FIELDS)
            rtde.start()

        sensor_fields = [
            "sample_index",
            "t_wall_ns",
            "t_monotonic_s",
            *KUNWEI_RAW_FIELDS,
            "fx_n_zeroed",
            "fy_n_zeroed",
            "fz_n_zeroed",
            "mx_nm_zeroed",
            "my_nm_zeroed",
            "mz_nm_zeroed",
            "normal_force_n",
            "force_norm_n",
            "torque_norm_nm",
            "frame_hex",
        ]
        bridge_fields = [
            "write_index",
            "t_wall_ns",
            "t_monotonic_s",
            "sensor_age_s",
            *INPUT_NAMES,
            "guard_reason",
            "baseline_ready",
            "baseline_epoch",
            "last_zero_request",
        ]
        bridge_output_fields = [f"ur_{field}_{idx}" for field in ["actual_TCP_pose", "actual_TCP_speed"] for idx in range(6)]
        bridge_output_fields += [
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

        with (
            sensor_csv_path.open("w", newline="", encoding="utf-8") as sensor_handle,
            bridge_csv_path.open("w", newline="", encoding="utf-8") as bridge_handle,
            raw_path.open("wb") as raw_handle,
        ):
            sensor_writer = csv.DictWriter(sensor_handle, fieldnames=sensor_fields)
            bridge_writer = csv.DictWriter(bridge_handle, fieldnames=bridge_fields + bridge_output_fields)
            sensor_writer.writeheader()
            bridge_writer.writeheader()

            while True:
                now = time.monotonic()
                if now - start_mono >= args.duration_s:
                    stop_reason = "duration"
                    break

                try:
                    chunk = sock.recv(8192)
                except socket.timeout:
                    chunk = b""
                if chunk:
                    buffer.extend(chunk)
                    frames, dropped = pop_frames(buffer, 0x48 if not args.no_start_command else None)
                    dropped_sync_bytes += dropped
                    for frame in frames:
                        try:
                            raw_values = parse_frame(frame)
                        except ValueError:
                            parse_errors += 1
                            continue
                        raw_handle.write(frame)
                        samples += 1
                        latest_frame_time = time.monotonic()
                        raw_si = [
                            raw_values[0] * FORCE_KG_TO_N,
                            raw_values[1] * FORCE_KG_TO_N,
                            raw_values[2] * FORCE_KG_TO_N,
                            raw_values[3] * MOMENT_KG_M_TO_NM,
                            raw_values[4] * MOMENT_KG_M_TO_NM,
                            raw_values[5] * MOMENT_KG_M_TO_NM,
                        ]
                        if not baseline_ready:
                            baseline_raw_si.append(raw_si)
                            target_baseline_s = args.baseline_s if baseline_epoch == 0 else args.rezero_s
                            if latest_frame_time - baseline_start_mono >= target_baseline_s:
                                baseline = [statistics.fmean(axis) for axis in zip(*baseline_raw_si)]
                                baseline_ready = True
                                zero_events.append(
                                    {
                                        "baseline_epoch": baseline_epoch,
                                        "completed_at_sample": samples,
                                        "completed_at_monotonic_s": latest_frame_time,
                                        "samples": len(baseline_raw_si),
                                        "duration_s": latest_frame_time - baseline_start_mono,
                                    }
                                )
                        latest_zeroed = [value - offset for value, offset in zip(raw_si, baseline)]
                        normal = normal_component(latest_zeroed, args.normal_axis, args.normal_sign)
                        force_norm = vec_norm(latest_zeroed[:3])
                        torque_norm = vec_norm(latest_zeroed[3:])
                        normals.append(normal)
                        force_norms.append(force_norm)
                        torque_norms.append(torque_norm)
                        sensor_writer.writerow(
                            {
                                "sample_index": samples,
                                "t_wall_ns": time.time_ns(),
                                "t_monotonic_s": f"{latest_frame_time:.9f}",
                                **{field: f"{value:.9g}" for field, value in zip(KUNWEI_RAW_FIELDS, raw_values)},
                                "fx_n_zeroed": f"{latest_zeroed[0]:.9g}",
                                "fy_n_zeroed": f"{latest_zeroed[1]:.9g}",
                                "fz_n_zeroed": f"{latest_zeroed[2]:.9g}",
                                "mx_nm_zeroed": f"{latest_zeroed[3]:.9g}",
                                "my_nm_zeroed": f"{latest_zeroed[4]:.9g}",
                                "mz_nm_zeroed": f"{latest_zeroed[5]:.9g}",
                                "normal_force_n": f"{normal:.9g}",
                                "force_norm_n": f"{force_norm:.9g}",
                                "torque_norm_nm": f"{torque_norm:.9g}",
                                "frame_hex": frame.hex(),
                            }
                        )
                elif sock.fileno() < 0:
                    stop_reason = "socket_closed"
                    break

                if rtde is not None:
                    sample = rtde.recv_available_sample(rtde_output_recipe, rtde_output_types)
                    if sample is not None:
                        latest_output = sample
                        zero_request = float(sample.get("output_double_register_34", 0.0))
                        if last_zero_request is None:
                            last_zero_request = zero_request
                        elif zero_request > last_zero_request:
                            baseline_epoch += 1
                            baseline_ready = False
                            baseline_raw_si = []
                            baseline_start_mono = time.monotonic()
                            last_zero_request = zero_request
                            zero_events.append(
                                {
                                    "baseline_epoch": baseline_epoch,
                                    "requested_at_monotonic_s": baseline_start_mono,
                                    "zero_request": zero_request,
                                }
                            )

                now = time.monotonic()
                if now >= next_write:
                    sensor_age = math.inf if latest_frame_time is None else now - latest_frame_time
                    sensor_ok = 1.0 if baseline_ready and sensor_age <= args.sensor_stale_s and parse_errors == 0 else 0.0
                    bridge_values = {
                        "normal_force_n": normal_component(latest_zeroed, args.normal_axis, args.normal_sign),
                        "force_norm_n": vec_norm(latest_zeroed[:3]),
                        "heartbeat": heartbeat,
                        "sensor_ok": sensor_ok,
                        "stop_request": stop_request,
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
                        guard_reason = guard_stop_reason(args, bridge_values)
                        if guard_reason is not None:
                            bridge_values["stop_request"] = 1.0
                            stop_request = 1.0
                            stop_reason = guard_reason
                    if rtde is not None:
                        rtde.send_input_sample(
                            rtde_input_recipe,
                            rtde_input_types,
                            [bridge_values[name] for name in INPUT_NAMES],
                        )
                    row = {
                        "write_index": bridge_writes + 1,
                        "t_wall_ns": time.time_ns(),
                        "t_monotonic_s": f"{now:.9f}",
                        "sensor_age_s": sensor_age if math.isfinite(sensor_age) else "",
                        **{key: f"{value:.9g}" for key, value in bridge_values.items()},
                        "guard_reason": guard_reason or "",
                        "baseline_ready": int(baseline_ready),
                        "baseline_epoch": baseline_epoch,
                        "last_zero_request": "" if last_zero_request is None else last_zero_request,
                    }
                    row.update(flatten_output(latest_output))
                    bridge_writer.writerow(row)
                    bridge_writes += 1
                    heartbeat += 1.0
                    next_write += write_period
                    if guard_reason is not None:
                        break
    finally:
        if sock is not None and not args.no_stop_command:
            try:
                sock.sendall(STOP_STREAM)
            except OSError:
                pass
        if sock is not None:
            sock.close()
        if rtde is not None:
            rtde.__exit__(None, None, None)

    summary = {
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "stop_reason": stop_reason,
        "samples": samples,
        "bridge_writes": bridge_writes,
        "parse_errors": parse_errors,
        "dropped_sync_bytes": dropped_sync_bytes,
        "baseline_ready": baseline_ready,
        "baseline_samples": len(baseline_raw_si),
        "baseline_epoch": baseline_epoch,
        "last_zero_request": last_zero_request,
        "zero_events": zero_events,
        "baseline_si_offsets": dict(zip(["fx_n", "fy_n", "fz_n", "mx_nm", "my_nm", "mz_nm"], baseline)),
        "normal_force_stats_n": stats(normals),
        "force_norm_stats_n": stats(force_norms),
        "torque_norm_stats_nm": stats(torque_norms),
        "paths": {
            "metadata": str(metadata_path),
            "summary": str(summary_path),
            "sensor_csv": str(sensor_csv_path),
            "bridge_csv": str(bridge_csv_path),
            "raw_frames": str(raw_path),
        },
    }
    write_json(summary_path, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if samples > 0 and parse_errors == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
