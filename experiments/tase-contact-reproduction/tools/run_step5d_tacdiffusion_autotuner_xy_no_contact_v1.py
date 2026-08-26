#!/usr/bin/env python3
"""Staged Direct-Torque impedance no-contact diagnostic.

This is an independent Step5d profile.  It owns one RTDE writer, sends only
the typed moving-reference command packet, and keeps all output as diagnostic
evidence.  It never loads/plays a TP program, performs contact search, trains,
qualifies, or promotes anything.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import socket
import sys
import time
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
VIC_ROOT = ROOT.parent / "ur10e-variable-impedance"
SENSOR_TOOLS = ROOT.parent / "sensor-integration/kunwei-kwr75b/tools"
REALSETUP_TOOLS = Path("/home/andy/codex-private-skills-shared-main/skills/ur10e-realsetup/scripts")
for path in (ROOT / "tools", SENSOR_TOOLS, REALSETUP_TOOLS, VIC_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from _ur_common import dashboard_exchange  # noqa: E402
from build_step4e_p0p1_programs import build_urp  # noqa: E402
from capture_kunwei_kwr75_1khz import (  # noqa: E402
    FORCE_KG_TO_N,
    MOMENT_KG_M_TO_NM,
    START_STREAM,
    STOP_STREAM,
    parse_frame,
    pop_frames,
)
from step5d_v34_transport_primitives import RTDEBridgeClient  # noqa: E402
from ur10e_parallel import ResourceProfile, writer_lease  # noqa: E402
from ur10e_vic.tacdiffusion.step5d_autotuner_xy_no_contact_v1 import (  # noqa: E402
    CBF_QP_HALF_AXES_M,
    CONTROL_RATE_HZ,
    DURATION_OPTIONS_S,
    HARD_ELLIPSE_HALF_AXES_M,
    KUNWEI_FORCE_LIMIT_N,
    KUNWEI_TORQUE_LIMIT_NM,
    LINEAR_COMMAND_LIMIT_M_S,
    ORIENTATION_LIMIT_RAD,
    ORIGIN_XY_M,
    P_LATERAL_XY,
    PROFILE_ID,
    PROTOCOL_TOKEN,
    QDOT_LIMIT_RAD_S,
    REFERENCE_SPEED_LIMIT_M_S,
    RUNTIME_SCHEMA,
    STATIC_FOOTPRINT_EPSILON_M,
    STATIC_SAFE_FRAME_XY_MAX_M,
    STATIC_SAFE_FRAME_XY_MIN_M,
    THETA_RATE_RAD_S,
    AMPLITUDE_M,
    U_ALONG_XY,
    ZERO_WRENCH,
    Z_DEVIATION_LIMIT_M,
    build_offline_evidence,
    build_runtime_source,
    package_identity,
    reference_at,
)


ROBOT_HOST_DEFAULT = "192.168.1.18"
SENSOR_HOST_DEFAULT = "192.168.50.25"
SENSOR_PORT_DEFAULT = 5152
RTDE_INPUT_FIELDS = [f"input_double_register_{index}" for index in range(24, 48)] + [
    f"input_int_register_{index}" for index in range(24, 36)
]
RTDE_OUTPUT_FIELDS = [
    "timestamp",
    "actual_TCP_pose",
    "actual_TCP_speed",
    "actual_q",
    "actual_qd",
    "target_moment",
    "joint_mode",
    "runtime_state",
    "robot_mode",
    "safety_mode",
    *[f"output_double_register_{index}" for index in range(24, 48)],
    *[f"output_int_register_{index}" for index in range(24, 36)],
]
RUNTIME_STOPPED = 1
RUNTIME_PLAYING = 2
ROBOT_MODE_RUNNING = 7
SAFETY_MODE_NORMAL = 1
MODE_RUN = 1
MODE_ABORT = 3
MODE_END = 2
LEASE_ID = 1
KUNWEI_BASELINE_SAMPLES = 1000


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _finite_pose(values: Sequence[float]) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) != 6 or not all(math.isfinite(value) for value in result):
        raise ValueError("TCP pose must contain six finite values")
    return result


def _duration(value: int | str) -> int:
    text = str(value).lower().removesuffix("s")
    result = int(text)
    if result not in DURATION_OPTIONS_S:
        raise ValueError(f"duration must be one of {DURATION_OPTIONS_S}")
    return result


def _read_rtde_sample(host: str, timeout_s: float = 3.0) -> dict[str, Any]:
    with RTDEBridgeClient(host, timeout=timeout_s) as rtde:
        rtde.negotiate()
        recipe, types = rtde.setup_outputs(10.0, [
            "timestamp", "actual_TCP_pose", "actual_TCP_speed", "actual_q",
            "actual_qd", "runtime_state", "robot_mode", "safety_mode",
        ])
        rtde.start()
        sample = rtde.receive_latest(recipe, types, [
            "timestamp", "actual_TCP_pose", "actual_TCP_speed", "actual_q",
            "actual_qd", "runtime_state", "robot_mode", "safety_mode",
        ], timeout_s)
        if sample is None:
            raise RuntimeError("RTDE status sample unavailable")
        return sample


def readonly_status(host: str, timeout_s: float = 3.0) -> dict[str, Any]:
    dashboard = dashboard_exchange(host, [
        "PolyscopeVersion", "get loaded program", "programState", "robotmode",
        "safetystatus", "is in remote control",
    ], timeout=timeout_s)
    sample = _read_rtde_sample(host, timeout_s)
    speed = tuple(float(value) for value in sample["actual_TCP_speed"])
    qd = tuple(float(value) for value in sample["actual_qd"])
    return {
        "dashboard": dashboard,
        "rtde": sample,
        "remote_control": dashboard.get("is in remote control", "").lower() == "true",
        "stopped": int(sample["runtime_state"]) == RUNTIME_STOPPED,
        "stationary": max((abs(value) for value in (*speed, *qd)), default=0.0) <= 1.0e-4,
        "motion_performed": False,
        "urscript_sent": False,
        "rtde_inputs_written": False,
        "kunwei_stream_started": False,
    }


def _send_urscript(host: str, source: str, timeout_s: float) -> None:
    with socket.create_connection((host, 30002), timeout=timeout_s) as connection:
        connection.sendall((source.rstrip() + "\n").encode("utf-8"))


def _reset_receiver_outputs(host: str, timeout_s: float) -> None:
    """Clear stale output-register markers before a fresh Secondary source."""

    source = """def step5d_receiver_output_reset():
  write_output_integer_register(24, 0)
  write_output_integer_register(25, 0)
  write_output_integer_register(26, 0)
  write_output_integer_register(27, 0)
  write_output_integer_register(28, 0)
  write_output_integer_register(29, 0)
  write_output_integer_register(30, 0)
  write_output_integer_register(31, 0)
  write_output_integer_register(32, 0)
  write_output_integer_register(33, 0)
  write_output_integer_register(34, 0)
  write_output_integer_register(35, 0)
  sync()
end
step5d_receiver_output_reset()
"""
    _send_urscript(host, source, timeout_s)


def _guarded_movel(host: str, target_pose: Sequence[float], timeout_s: float) -> None:
    pose = _finite_pose(target_pose)
    script = (
        "def step5d_xy_preposition():\n"
        f"  movel(p[{', '.join(f'{value:.17g}' for value in pose)}], a=0.05, v=0.003, r=0.0)\n"
        "  stopl(0.1)\n"
        "end\n"
        "step5d_xy_preposition()\n"
    )
    _send_urscript(host, script, timeout_s)


class KunweiStream:
    """Single owner for the KWR75 stream and raw/summary evidence."""

    def __init__(self, host: str, port: int, output_dir: Path, timeout_s: float) -> None:
        self.host = host
        self.port = port
        self.output_dir = output_dir
        self.timeout_s = timeout_s
        self.sock: socket.socket | None = None
        self.buffer = bytearray()
        self.raw = output_dir / "kunwei_raw_frames.bin"
        self.rows = 0
        self.dropped = 0
        self.parse_errors = 0
        self.last_wrench = ZERO_WRENCH
        self.last_raw_wrench = ZERO_WRENCH
        self._baseline_sum = [0.0] * 6
        self.baseline_si_offsets: tuple[float, ...] | None = None
        self.last_receive_mono: float | None = None
        self.first_receive_mono: float | None = None
        self._raw_handle: Any | None = None

    def __enter__(self) -> "KunweiStream":
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
        self.sock.setblocking(False)
        self._raw_handle = self.raw.open("wb")
        self.sock.sendall(START_STREAM)
        return self

    def poll(self) -> tuple[float, ...]:
        if self.sock is None:
            raise RuntimeError("Kunwei stream is not open")
        while True:
            try:
                chunk = self.sock.recv(8192)
            except BlockingIOError:
                break
            if not chunk:
                raise RuntimeError("Kunwei stream closed")
            self.buffer.extend(chunk)
            if self._raw_handle is not None:
                self._raw_handle.write(chunk)
        frames, dropped = pop_frames(self.buffer, None)
        self.dropped += dropped
        for frame in frames:
            try:
                values = parse_frame(frame)
            except ValueError:
                self.parse_errors += 1
                continue
            self.rows += 1
            now = time.monotonic()
            self.first_receive_mono = now if self.first_receive_mono is None else self.first_receive_mono
            self.last_receive_mono = now
            raw_wrench = tuple(
                float(value) * (FORCE_KG_TO_N if index < 3 else MOMENT_KG_M_TO_NM)
                for index, value in enumerate(values)
            )
            self.last_raw_wrench = raw_wrench
            if self.baseline_si_offsets is None:
                for index, value in enumerate(raw_wrench):
                    self._baseline_sum[index] += value
                if self.rows >= KUNWEI_BASELINE_SAMPLES:
                    self.baseline_si_offsets = tuple(
                        value / float(KUNWEI_BASELINE_SAMPLES)
                        for value in self._baseline_sum
                    )
                self.last_wrench = ZERO_WRENCH
            else:
                self.last_wrench = tuple(
                    value - offset
                    for value, offset in zip(raw_wrench, self.baseline_si_offsets)
                )
        return self.last_wrench

    def assert_fresh(self, max_age_s: float) -> tuple[float, ...]:
        wrench = self.poll()
        if self.baseline_si_offsets is None:
            raise RuntimeError("Kunwei software baseline not ready")
        if self.last_receive_mono is None or time.monotonic() - self.last_receive_mono > max_age_s:
            raise RuntimeError("Kunwei stream stale")
        force = math.sqrt(sum(value * value for value in wrench[:3]))
        torque = math.sqrt(sum(value * value for value in wrench[3:]))
        if force > KUNWEI_FORCE_LIMIT_N or torque > KUNWEI_TORQUE_LIMIT_NM:
            raise RuntimeError(f"Kunwei guard exceeded force={force} torque={torque}")
        return wrench

    def summary(self) -> dict[str, Any]:
        duration = (
            None if self.first_receive_mono is None or self.last_receive_mono is None
            else self.last_receive_mono - self.first_receive_mono
        )
        return {
            "sensor_ip": self.host,
            "sensor_port": self.port,
            "stream_command": START_STREAM.hex(" ").upper(),
            "stop_command": STOP_STREAM.hex(" ").upper(),
            "samples": self.rows,
            "software_baseline_samples": KUNWEI_BASELINE_SAMPLES,
            "baseline_ready": self.baseline_si_offsets is not None,
            "baseline_si_offsets": None if self.baseline_si_offsets is None else list(self.baseline_si_offsets),
            "last_raw_wrench_tcp_si": list(self.last_raw_wrench),
            "duration_first_last_s": duration,
            "rate_hz": None if not duration or self.rows < 2 else (self.rows - 1) / duration,
            "dropped_sync_bytes": self.dropped,
            "parse_errors": self.parse_errors,
            "last_wrench_tcp_si": list(self.last_wrench),
            "raw_frames": str(self.raw),
        }

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.sock is not None:
            try:
                self.sock.sendall(STOP_STREAM)
            except OSError:
                pass
            self.sock.close()
            self.sock = None
        if self._raw_handle is not None:
            self._raw_handle.flush()
            self._raw_handle.close()
            self._raw_handle = None


def _command_values(
    *, mode: int, sequence: int, pose: Sequence[float], wrench: Sequence[float],
) -> list[Any]:
    desired = _finite_pose(pose)
    guard = tuple(float(value) for value in wrench)
    if len(guard) != 6 or not all(math.isfinite(value) for value in guard):
        raise ValueError("Kunwei wrench must contain six finite values")
    doubles = [*desired, 600.0, 600.0, 600.0, 30.0, 30.0, 30.0, *guard, *ZERO_WRENCH]
    # Dedicated profile protocol: int28 is the joint-mode acknowledgement and
    # int29 is the profile protocol echo consumed by the receiver.
    integers = [int(mode), int(sequence), int(sequence), LEASE_ID, 1, PROTOCOL_TOKEN, 0, 0, 0, 0, 0, 0]
    return doubles + integers


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _trajectory_identity(duration_s: int, source_sha: str, origin_pose: Sequence[float]) -> dict[str, Any]:
    """Return the frozen reference identity for both success and fail-closed runs."""

    return {
        "profile_id": PROFILE_ID,
        "duration_s": duration_s,
        "source_sha256": source_sha,
        "reference": {
            "origin_xy_m": list(ORIGIN_XY_M),
            "u_along_xy": list(U_ALONG_XY),
            "p_lateral_xy": list(P_LATERAL_XY),
            "theta": f"{THETA_RATE_RAD_S:g}*t",
            "along": f"{AMPLITUDE_M:g}*(theta-sin(theta))",
            "lateral": f"{AMPLITUDE_M:g}*(1-cos(theta))",
            "endpoint_t_s": 60.0,
            "endpoint_along_m": AMPLITUDE_M * (THETA_RATE_RAD_S * 60.0 - math.sin(THETA_RATE_RAD_S * 60.0)),
            "lateral_peak_m": 2.0 * AMPLITUDE_M,
            "fixed_z_m": float(origin_pose[2]),
            "anchor_orientation_rotvec": [float(value) for value in origin_pose[3:6]],
        },
    }


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def _pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right) or len(left) < 3:
        return None
    mean_left = sum(left) / len(left)
    mean_right = sum(right) / len(right)
    centered_left = [value - mean_left for value in left]
    centered_right = [value - mean_right for value in right]
    denominator = math.sqrt(
        sum(value * value for value in centered_left)
        * sum(value * value for value in centered_right)
    )
    if denominator <= 1.0e-12:
        return None
    return sum(a * b for a, b in zip(centered_left, centered_right)) / denominator


def _best_velocity_lag(
    desired: Sequence[float], actual: Sequence[float], sample_period_s: float
) -> tuple[float | None, float | None]:
    if len(desired) != len(actual) or len(desired) < 8:
        return None, None
    max_lag = min(200, max(1, len(desired) // 4))
    best_score: float | None = None
    best_lag: int | None = None
    for lag in range(-max_lag, max_lag + 1):
        if lag < 0:
            left = desired[-lag:]
            right = actual[: len(actual) + lag]
        elif lag > 0:
            left = desired[:-lag]
            right = actual[lag:]
        else:
            left = desired
            right = actual
        score = _pearson(left, right)
        if score is not None and (best_score is None or score > best_score):
            best_score = score
            best_lag = lag
    return best_score, None if best_lag is None else best_lag * sample_period_s


def _analysis_path_rows(rows: Sequence[Mapping[str, Any]], duration_s: int) -> list[Mapping[str, Any]]:
    """Select current-episode path rows and exclude pre-marker register residue."""

    path = [
        row for row in rows
        if int(row["protocol_echo"]) == PROTOCOL_TOKEN
        and int(row["receiver_state"]) in (1, 2)
        and float(row["t_s"]) <= float(duration_s) + 0.05
    ]
    return path


def _analysis_fault_codes(rows: Sequence[Mapping[str, Any]], duration_s: int) -> list[int]:
    """Include a current episode's terminal fault without stale reset residue."""

    return sorted({
        int(row["fault"])
        for row in rows
        if int(row["protocol_echo"]) == PROTOCOL_TOKEN
        and int(row["receiver_state"]) in (1, 2, 4)
        and float(row["t_s"]) <= float(duration_s) + 0.05
    })


def _analyze_and_plot(output_dir: Path, rows: Sequence[Mapping[str, Any]], duration_s: int) -> dict[str, Any] | None:
    """Write tracking metrics and the required diagnostic figures when rows exist."""

    path_rows = _analysis_path_rows(rows, duration_s)
    if len(path_rows) < 2:
        return None
    t = [float(row["t_s"]) for row in path_rows]
    desired_x = [float(row["desired_x_m"]) for row in path_rows]
    desired_y = [float(row["desired_y_m"]) for row in path_rows]
    actual_x = [float(row["actual_x_m"]) for row in path_rows]
    actual_y = [float(row["actual_y_m"]) for row in path_rows]
    desired_z = [float(row["desired_z_m"]) for row in path_rows]
    actual_z = [float(row["actual_z_m"]) for row in path_rows]
    desired_vx = [float(row["desired_vx_m_s"]) for row in path_rows]
    desired_vy = [float(row["desired_vy_m_s"]) for row in path_rows]
    actual_vx = [float(row["actual_vx_m_s"]) for row in path_rows]
    actual_vy = [float(row["actual_vy_m_s"]) for row in path_rows]
    errors = [math.hypot(x - dx, y - dy) for x, y, dx, dy in zip(actual_x, actual_y, desired_x, desired_y)]
    xy_step = [math.hypot(b - a, d - c) for a, b, c, d in zip(actual_x, actual_x[1:], actual_y, actual_y[1:])]
    desired_step = [math.hypot(b - a, d - c) for a, b, c, d in zip(desired_x, desired_x[1:], desired_y, desired_y[1:])]
    tail_start = max(0, int(len(path_rows) * 0.8) - 1)
    desired_tail = sum(desired_step[tail_start:])
    actual_tail = sum(xy_step[tail_start:])
    desired_length = sum(desired_step)
    actual_length = sum(xy_step)
    sample_period_s = (
        (t[-1] - t[0]) / float(len(t) - 1)
        if len(t) > 1 and t[-1] > t[0]
        else 1.0 / CONTROL_RATE_HZ
    )
    corr_x, lag_x = _best_velocity_lag(desired_vx, actual_vx, sample_period_s)
    corr_y, lag_y = _best_velocity_lag(desired_vy, actual_vy, sample_period_s)
    metrics: dict[str, Any] = {
        "schema": "step5d_tacdiffusion_autotuner_xy_tracking_metrics/v1",
        "duration_s": duration_s,
        "path_rows": len(path_rows),
        "timeline_s": {"start": t[0], "end": t[-1], "span": t[-1] - t[0]},
        "endpoint_error_xy_m": errors[-1],
        "peak_error_xy_m": max(errors),
        "rmse_error_xy_m": math.sqrt(sum(value * value for value in errors) / len(errors)),
        "desired_path_length_m": desired_length,
        "actual_path_length_m": actual_length,
        "overall_path_ratio": None if desired_length <= 1.0e-12 else actual_length / desired_length,
        "desired_tail_path_length_m": desired_tail,
        "actual_tail_path_length_m": actual_tail,
        "tail_path_ratio": None if desired_tail <= 1.0e-12 else actual_tail / desired_tail,
        "velocity_correlation": {"x": corr_x, "y": corr_y},
        "velocity_lag_s": {"x": lag_x, "y": lag_y},
        "observed_fault_codes": _analysis_fault_codes(rows, duration_s),
        "diagnostic_only": True,
    }
    _write_json(output_dir / "tracking_metrics.json", metrics)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return metrics

    def save_figure(filename: str, figure: Any) -> None:
        figure.savefig(output_dir / filename, dpi=160, bbox_inches="tight")
        plt.close(figure)

    fig, ax = plt.subplots(figsize=(6.0, 6.0))
    ax.plot(actual_x, actual_y, color="#1f77b4", linewidth=1.2)
    ax.scatter([actual_x[0], actual_x[-1]], [actual_y[0], actual_y[-1]], c=["#2ca02c", "#d62728"], s=24)
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("X (m)")
    ax.set_ylabel("Y (m)")
    ax.set_title("Actual XY path")
    ax.grid(True, alpha=0.3)
    save_figure("xy_actual_only.png", fig)

    fig, ax = plt.subplots(figsize=(6.0, 6.0))
    ax.plot(desired_x, desired_y, "--", color="#ff7f0e", linewidth=1.2, label="desired")
    ax.plot(actual_x, actual_y, color="#1f77b4", linewidth=1.2, label="actual")
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("X (m)")
    ax.set_ylabel("Y (m)")
    ax.set_title("Desired vs actual XY")
    ax.legend()
    ax.grid(True, alpha=0.3)
    save_figure("xy_desired_vs_actual.png", fig)

    fig, ax = plt.subplots(figsize=(8.0, 4.0))
    ax.plot(t, desired_x, "--", label="desired X")
    ax.plot(t, actual_x, label="actual X")
    ax.plot(t, desired_y, "--", label="desired Y")
    ax.plot(t, actual_y, label="actual Y")
    ax.plot(t, desired_z, "--", label="desired Z")
    ax.plot(t, actual_z, label="actual Z")
    ax.set_xlabel("t (s)")
    ax.set_ylabel("base position (m)")
    ax.set_title("X / Y / Z timeline")
    ax.legend(ncol=3, fontsize=8)
    ax.grid(True, alpha=0.3)
    save_figure("xyz_time.png", fig)

    fig, ax = plt.subplots(figsize=(8.0, 4.0))
    ax.plot(t, [value * 1000.0 for value in errors], color="#9467bd", label="XY error")
    ax.set_xlabel("t (s)")
    ax.set_ylabel("path error (mm)")
    ax.set_title("XY path error")
    ax.grid(True, alpha=0.3)
    ax.legend()
    save_figure("path_error.png", fig)
    return metrics


def build_package(output_dir: Path) -> dict[str, Any]:
    """Create the exact local script/txt/urp triplet without controller I/O."""

    output_dir.mkdir(parents=True, exist_ok=False)
    source = build_runtime_source(duration_s=60, entrypoint_suffix="package")
    program = PROFILE_ID
    controller_dir = "/programs/andyl/kunwei/step5"
    script_path = output_dir / f"{program}.script"
    txt_path = output_dir / f"{program}.txt"
    urp_path = output_dir / f"{program}.urp"
    script_path.write_text(source, encoding="utf-8")
    txt_path.write_text(
        f"{PROFILE_ID}\n\nIndependent 60 s no-contact Direct Torque Cartesian impedance diagnostic.\n"
        "No contact search, training, qualification, promotion, or TP Play.\n",
        encoding="utf-8",
    )
    urp_path.write_bytes(build_urp(source, program, controller_dir))
    manifest = {
        "schema": "step5d_tacdiffusion_autotuner_xy_no_contact_package/v1",
        "profile_id": PROFILE_ID,
        "controller_target": f"{controller_dir}/{program}.urp",
        "claim_class": "offline_package_not_uploaded",
        "files": {
            "script": {"path": str(script_path), "sha256": _sha256(script_path)},
            "txt": {"path": str(txt_path), "sha256": _sha256(txt_path)},
            "urp": {"path": str(urp_path), "sha256": _sha256(urp_path)},
        },
        "source_identity": package_identity(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(output_dir / "package_manifest.json", manifest)
    return manifest


def run_live(args: argparse.Namespace) -> dict[str, Any]:
    duration = _duration(args.duration)
    status = readonly_status(args.robot_host, args.connect_timeout_s)
    dashboard = status["dashboard"]
    rtde_status = status["rtde"]
    failures: list[str] = []
    if not status["remote_control"]:
        failures.append("remote_control_required")
    if not status["stopped"]:
        failures.append("program_must_be_stopped")
    if not status["stationary"]:
        failures.append("robot_must_be_stationary")
    if dashboard.get("safetystatus") != "Safetystatus: NORMAL":
        failures.append("dashboard_safety_not_normal")
    if int(rtde_status["robot_mode"]) != ROBOT_MODE_RUNNING or int(rtde_status["safety_mode"]) != SAFETY_MODE_NORMAL:
        failures.append("RTDE_robot_or_safety_mode_not_ready")
    if failures:
        raise RuntimeError("live_preflight_failed:" + ",".join(failures))

    session_home_pose = _finite_pose(rtde_status["actual_TCP_pose"])
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    source = build_runtime_source(duration_s=duration)
    source_sha = _sha256_bytes(source.encode("utf-8"))
    identity = package_identity()
    # The package source and the live Secondary source intentionally have
    # distinct receiver entrypoints to avoid controller-function collisions.
    # Preserve both hashes instead of overwriting the package read-back hash.
    identity = {
        **identity,
        "live_runtime_source_sha256": source_sha,
        "duration_s": duration,
    }
    # A no-op preposition is still recorded as a guarded operation; if the
    # current pose is outside the exact origin tolerance we use one bounded
    # 3 mm/s Cartesian move and wait for stationary before starting torque.
    origin = reference_at(0.0, startup_safe_z_m=session_home_pose[2], anchor_orientation_rotvec=session_home_pose[3:])
    prepositioned = math.dist(session_home_pose[:3], origin.pose_base[:3]) <= 0.0005
    if not prepositioned:
        _guarded_movel(args.robot_host, origin.pose_base, args.connect_timeout_s)
        deadline = time.monotonic() + args.preposition_timeout_s
        while time.monotonic() < deadline:
            sample = _read_rtde_sample(args.robot_host, args.connect_timeout_s)
            if math.dist(tuple(sample["actual_TCP_pose"][:3]), origin.pose_base[:3]) <= 0.0005 and max(abs(float(v)) for v in sample["actual_TCP_speed"]) <= 0.001:
                prepositioned = True
                break
            time.sleep(0.02)
    if not prepositioned:
        raise RuntimeError("guarded_preposition_timeout")

    rows: list[dict[str, Any]] = []
    fault: str | None = None
    sequence = 0
    receiver_terminal_state: int | None = None
    start_mono: float | None = None
    deadline_s: float | None = None
    last_output_mono = time.monotonic()
    next_send = last_output_mono
    receiver_fault_code = 0
    with writer_lease(
        ResourceProfile.from_env(),
        "step5d-tacdiffusion-autotuner-xy-no-contact",
        blocking=False,
    ):
        with KunweiStream(args.sensor_ip, args.sensor_port, output_dir, args.connect_timeout_s) as kunwei:
            kunwei_deadline = time.monotonic() + args.kunwei_start_timeout_s
            while kunwei.baseline_si_offsets is None and time.monotonic() < kunwei_deadline:
                kunwei.poll()
                if kunwei.baseline_si_offsets is None:
                    time.sleep(0.001)
            if kunwei.baseline_si_offsets is None:
                raise RuntimeError("Kunwei software baseline preflight timeout")
            with RTDEBridgeClient(args.robot_host, timeout=args.connect_timeout_s) as rtde:
                rtde.negotiate()
                out_recipe, out_types = rtde.setup_outputs(CONTROL_RATE_HZ, RTDE_OUTPUT_FIELDS)
                in_recipe, in_types = rtde.setup_inputs(RTDE_INPUT_FIELDS)
                rtde.start()
                try:
                    # Establish an explicit zero/wait packet before starting
                    # Secondary source so stale registers from another program
                    # cannot be interpreted as the first command.
                    rtde.send_inputs(
                        in_recipe,
                        in_types,
                        _command_values(mode=0, sequence=0, pose=origin.pose_base, wrench=ZERO_WRENCH),
                    )
                    _reset_receiver_outputs(args.robot_host, args.connect_timeout_s)
                    time.sleep(0.02)
                    # Start the receiver only after RTDE is live.  Its phase-0
                    # wait state holds zero torque until the first coherent
                    # typed packet arrives; no TP Load/Play is involved.
                    _send_urscript(args.robot_host, source, args.connect_timeout_s)
                    last_output_mono = time.monotonic()
                    receiver_start_deadline = time.monotonic() + min(args.return_timeout_s, 5.0)
                    receiver_started = False
                    receiver_marker_cleared = False
                    while time.monotonic() < receiver_start_deadline and not receiver_started:
                        samples = rtde.receive_available(out_recipe, out_types, RTDE_OUTPUT_FIELDS, 0.05)
                        for sample in samples:
                            receiver_terminal_state = int(sample["output_int_register_24"])
                            last_output_mono = time.monotonic()
                            row = _live_row(sample, 0.0, duration, ZERO_WRENCH, source_sha, identity["package_id"], session_home_pose)
                            rows.append(row)
                            protocol_echo = int(sample["output_int_register_28"])
                            if protocol_echo == 0:
                                receiver_marker_cleared = True
                            receiver_started = receiver_marker_cleared and protocol_echo == PROTOCOL_TOKEN
                            # Ignore stale output-register faults from a prior
                            # Secondary source until this episode's protocol
                            # marker is observed; faults after the marker are
                            # authoritative for the current receiver.
                            if receiver_started:
                                receiver_fault_code = max(receiver_fault_code, int(row["fault"]))
                        if not receiver_started:
                            time.sleep(0.002)
                    if not receiver_started:
                        raise RuntimeError("receiver_start_marker_missing")
                    if receiver_fault_code:
                        raise RuntimeError(f"receiver_fault_code:{receiver_fault_code}")
                    path_transition_deadline: float | None = None
                    # The marker handshake can consume an arbitrary fraction
                    # of a control period.  Re-anchor the packet deadline at
                    # the first PATH tick so the strict +1 sequence contract
                    # cannot be violated by a catch-up burst.
                    next_send = time.monotonic()
                    while receiver_terminal_state != 5:
                        now = time.monotonic()
                        if start_mono is None:
                            start_mono = now
                            deadline_s = start_mono + duration
                        elapsed = now - start_mono
                        wrench = kunwei.assert_fresh(args.kunwei_watchdog_s)
                        if elapsed >= duration and path_transition_deadline is None:
                            path_transition_deadline = now + args.return_timeout_s
                        if path_transition_deadline is not None and now >= path_transition_deadline:
                            raise RuntimeError("receiver_path_transition_timeout")
                        if now < next_send:
                            samples = rtde.receive_available(out_recipe, out_types, RTDE_OUTPUT_FIELDS, min(next_send - now, 0.001))
                            for sample in samples:
                                last_output_mono = time.monotonic()
                                receiver_terminal_state = int(sample["output_int_register_24"])
                                row = _live_row(sample, elapsed, duration, wrench, source_sha, identity["package_id"], session_home_pose)
                                rows.append(row)
                                receiver_fault_code = max(receiver_fault_code, int(row["fault"]))
                                if receiver_fault_code:
                                    raise RuntimeError(f"receiver_fault_code:{receiver_fault_code}")
                            continue
                        sequence += 1
                        point = reference_at(min(max(elapsed, 0.0), float(duration)), startup_safe_z_m=session_home_pose[2], anchor_orientation_rotvec=session_home_pose[3:])
                        rtde.send_inputs(in_recipe, in_types, _command_values(mode=MODE_RUN, sequence=sequence, pose=point.pose_base, wrench=wrench))
                        # Re-anchor after every send.  Accumulating a stale
                        # deadline would create a catch-up burst after one
                        # delayed tick and make the receiver observe a skipped
                        # sequence, which must fail closed.
                        next_send = time.monotonic() + 1.0 / CONTROL_RATE_HZ
                        samples = rtde.receive_available(out_recipe, out_types, RTDE_OUTPUT_FIELDS, 0.002)
                        for sample in samples:
                            last_output_mono = time.monotonic()
                            receiver_terminal_state = int(sample["output_int_register_24"])
                            row = _live_row(sample, elapsed, duration, wrench, source_sha, identity["package_id"], session_home_pose)
                            rows.append(row)
                            receiver_fault_code = max(receiver_fault_code, int(row["fault"]))
                            if receiver_fault_code:
                                raise RuntimeError(f"receiver_fault_code:{receiver_fault_code}")
                        if time.monotonic() - last_output_mono > args.rtde_watchdog_s:
                            raise RuntimeError("RTDE output stale")
                    # Receiver source performs the typed path-origin return; wait
                    # for its terminal state, then host owns guarded session Home.
                    end_deadline = time.monotonic() + args.return_timeout_s
                    while receiver_terminal_state != 5 and time.monotonic() < end_deadline:
                        wrench = kunwei.assert_fresh(args.kunwei_watchdog_s)
                        samples = rtde.receive_available(out_recipe, out_types, RTDE_OUTPUT_FIELDS, 0.01)
                        for sample in samples:
                            receiver_terminal_state = int(sample["output_int_register_24"])
                            row = _live_row(sample, duration, duration, wrench, source_sha, identity["package_id"], session_home_pose)
                            rows.append(row)
                            receiver_fault_code = max(receiver_fault_code, int(row["fault"]))
                            if receiver_fault_code:
                                raise RuntimeError(f"receiver_fault_code:{receiver_fault_code}")
                        time.sleep(0.002)
                    if receiver_terminal_state != 5:
                        raise RuntimeError("receiver_path_origin_return_timeout")
                except Exception as exc:
                    fault = f"{type(exc).__name__}: {exc}"
                    try:
                        sequence += 1
                        rtde.send_inputs(in_recipe, in_types, _command_values(mode=MODE_ABORT, sequence=sequence, pose=origin.pose_base, wrench=kunwei.last_wrench))
                    except Exception:
                        pass
                    try:
                        _write_csv(output_dir / "direct_torque_rtde.csv", rows)
                        analysis = _analyze_and_plot(output_dir, rows, duration)
                        trajectory_path = output_dir / "trajectory_identity.json"
                        _write_json(trajectory_path, _trajectory_identity(duration, source_sha, origin.pose_base))
                        _write_json(output_dir / "evidence.json", {
                            "schema": "step5d_tacdiffusion_autotuner_xy_no_contact_evidence/v1",
                            "claim": "single_autotuner_xy_no_contact_diagnostic",
                            "profile_id": PROFILE_ID,
                            "ok": False,
                            "duration_s": duration,
                            "control_rate_hz": CONTROL_RATE_HZ,
                            "sample_count": len(rows),
                            "source_sha256": source_sha,
                            "package_identity": identity,
                            "session_home_pose": list(session_home_pose),
                            "flags": {"contact": False, "training_dataset": False, "qualification": False, "promotion": False, "tracking_failure_diagnostic_only": True},
                            "motion": {"prepositioned": prepositioned, "path": "full_xy_cycloid", "direct_torque": True, "cartesian_impedance": True},
                            "kunwei": kunwei.summary(),
                            "terminal_safety_snapshot": {"path_origin": False, "session_home": False, "stop": True, "stationary_verified": False, "receiver_fault_code": receiver_fault_code, "fault": fault},
                            "analysis": None if analysis is None else {"metrics": str(output_dir / "tracking_metrics.json"), "figures": [
                                str(output_dir / "xy_actual_only.png"),
                                str(output_dir / "xy_desired_vs_actual.png"),
                                str(output_dir / "xyz_time.png"),
                                str(output_dir / "path_error.png"),
                            ]},
                            "csv": str(output_dir / "direct_torque_rtde.csv"),
                            "trajectory_identity": str(trajectory_path),
                            "created_at": datetime.now(timezone.utc).isoformat(),
                        })
                    except Exception:
                        pass
                    raise
            # Host Home is a separate guarded Cartesian move after receiver stop.
            _guarded_movel(args.robot_host, session_home_pose, args.connect_timeout_s)
            home_deadline = time.monotonic() + args.return_timeout_s
            stationary = False
            while time.monotonic() < home_deadline:
                sample = _read_rtde_sample(args.robot_host, args.connect_timeout_s)
                if math.dist(tuple(sample["actual_TCP_pose"][:3]), session_home_pose[:3]) <= 0.0005 and max(abs(float(v)) for v in (*sample["actual_TCP_speed"], *sample["actual_qd"])) <= 0.001:
                    stationary = True
                    break
                time.sleep(0.02)
            if not stationary:
                raise RuntimeError("session_home_stationary_verification_failed")

    csv_path = output_dir / "direct_torque_rtde.csv"
    _write_csv(csv_path, rows)
    analysis = _analyze_and_plot(output_dir, rows, duration)
    kunwei_summary = kunwei.summary()
    terminal_snapshot = {
        "path_origin": receiver_terminal_state == 5,
        "session_home": True,
        "stop": True,
        "stationary_verified": True,
        "receiver_fault_code": receiver_fault_code,
        "fault": fault,
    }
    evidence = {
        "schema": "step5d_tacdiffusion_autotuner_xy_no_contact_evidence/v1",
        "claim": "single_autotuner_xy_no_contact_diagnostic",
        "profile_id": PROFILE_ID,
        "runtime_schema": RUNTIME_SCHEMA,
        "ok": fault is None and receiver_fault_code == 0 and receiver_terminal_state == 5 and bool(rows),
        "duration_s": duration,
        "control_rate_hz": CONTROL_RATE_HZ,
        "sample_count": len(rows),
        "source_sha256": source_sha,
        "package_identity": identity,
        "session_home_pose": list(session_home_pose),
        "flags": {"contact": False, "training_dataset": False, "qualification": False, "promotion": False, "tracking_failure_diagnostic_only": True},
        "motion": {"prepositioned": prepositioned, "path": "full_xy_cycloid", "direct_torque": True, "cartesian_impedance": True},
        "kunwei": kunwei_summary,
        "terminal_safety_snapshot": terminal_snapshot,
        "analysis": None if analysis is None else {"metrics": str(output_dir / "tracking_metrics.json"), "figures": [
            str(output_dir / "xy_actual_only.png"),
            str(output_dir / "xy_desired_vs_actual.png"),
            str(output_dir / "xyz_time.png"),
            str(output_dir / "path_error.png"),
        ]},
        "csv": str(csv_path),
        "trajectory_identity": str(output_dir / "trajectory_identity.json"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(output_dir / "evidence.json", evidence)
    _write_json(output_dir / "trajectory_identity.json", _trajectory_identity(duration, source_sha, origin.pose_base))
    return evidence


def _live_row(sample: Mapping[str, Any], elapsed: float, duration_s: int, wrench: Sequence[float], source_sha: str, package_id: str, session_home_pose: Sequence[float]) -> dict[str, Any]:
    actual = tuple(float(value) for value in sample["actual_TCP_pose"])
    actual_speed = tuple(float(value) for value in sample["actual_TCP_speed"])
    qd = tuple(float(value) for value in sample["actual_qd"])
    desired = reference_at(min(max(elapsed, 0.0), float(duration_s)), startup_safe_z_m=float(session_home_pose[2]), anchor_orientation_rotvec=session_home_pose[3:])
    error = (actual[0] - desired.pose_base[0], actual[1] - desired.pose_base[1])
    along = error[0] * (-0.010785642631908187) + error[1] * 0.9999418332648238
    lateral = error[0] * (-0.9999418332648239) + error[1] * (-0.010785642631908406)
    hard_value = (along / HARD_ELLIPSE_HALF_AXES_M[0]) ** 2 + (lateral / HARD_ELLIPSE_HALF_AXES_M[1]) ** 2
    cbf_value = (along / CBF_QP_HALF_AXES_M[0]) ** 2 + (lateral / CBF_QP_HALF_AXES_M[1]) ** 2
    static_frame_ok = (
        STATIC_SAFE_FRAME_XY_MIN_M[0] - STATIC_FOOTPRINT_EPSILON_M <= desired.pose_base[0] <= STATIC_SAFE_FRAME_XY_MAX_M[0] + STATIC_FOOTPRINT_EPSILON_M
        and STATIC_SAFE_FRAME_XY_MIN_M[1] - STATIC_FOOTPRINT_EPSILON_M <= desired.pose_base[1] <= STATIC_SAFE_FRAME_XY_MAX_M[1] + STATIC_FOOTPRINT_EPSILON_M
    )
    actual_linear_speed = math.sqrt(sum(value * value for value in actual_speed[:3]))
    reference_speed = math.sqrt(sum(value * value for value in desired.velocity_base[:3]))
    orientation_error = math.sqrt(sum((actual[index + 3] - session_home_pose[index + 3]) ** 2 for index in range(3)))
    z_deviation = abs(actual[2] - session_home_pose[2])
    fault = int(sample["output_int_register_26"])
    protocol_echo = int(sample["output_int_register_28"])
    return {
        "t_s": elapsed,
        "controller_timestamp_s": float(sample["timestamp"]),
        "desired_x_m": desired.pose_base[0], "desired_y_m": desired.pose_base[1], "desired_z_m": desired.pose_base[2],
        "actual_x_m": actual[0], "actual_y_m": actual[1], "actual_z_m": actual[2],
        "desired_vx_m_s": desired.velocity_base[0], "desired_vy_m_s": desired.velocity_base[1],
        "actual_vx_m_s": actual_speed[0], "actual_vy_m_s": actual_speed[1],
        "local_along_reference_m": desired.along_m, "local_lateral_reference_m": desired.lateral_m,
        "local_along_error_m": along, "local_lateral_error_m": lateral,
        "guard_static_safe_frame_ok": static_frame_ok,
        "guard_hard_ellipse_value": hard_value, "guard_hard_ellipse_ok": hard_value <= 1.0 + 1.0e-12,
        "guard_cbf_qp_value": cbf_value, "guard_cbf_qp_ok": cbf_value <= 1.0 + 1.0e-12,
        "kunwei_force_n": math.sqrt(sum(float(value) ** 2 for value in wrench[:3])),
        "kunwei_torque_nm": math.sqrt(sum(float(value) ** 2 for value in wrench[3:])),
        "guard_kunwei_force_ok": math.sqrt(sum(float(value) ** 2 for value in wrench[:3])) <= KUNWEI_FORCE_LIMIT_N,
        "guard_kunwei_torque_ok": math.sqrt(sum(float(value) ** 2 for value in wrench[3:])) <= KUNWEI_TORQUE_LIMIT_NM,
        "guard_z_deviation_m": z_deviation, "guard_z_ok": z_deviation <= Z_DEVIATION_LIMIT_M,
        "guard_orientation_error_rad": orientation_error, "guard_orientation_ok": orientation_error <= ORIENTATION_LIMIT_RAD,
        "guard_reference_speed_m_s": reference_speed, "guard_reference_speed_ok": reference_speed <= REFERENCE_SPEED_LIMIT_M_S,
        "guard_actual_linear_speed_m_s": actual_linear_speed, "guard_linear_command_ok": actual_linear_speed <= LINEAR_COMMAND_LIMIT_M_S,
        "actual_qd_max_rad_s": max(abs(value) for value in qd), "guard_qdot_ok": max(abs(value) for value in qd) <= QDOT_LIMIT_RAD_S,
        "receiver_state": int(sample["output_int_register_24"]), "ack_sequence": int(sample["output_int_register_25"]), "fault": fault,
        "protocol_echo": protocol_echo, "guard_protocol_ok": protocol_echo == PROTOCOL_TOKEN, "guard_receiver_fault_ok": fault == 0,
        "source_sha256": source_sha, "package_id": package_id,
        "runtime_state": int(sample["runtime_state"]), "robot_mode": int(sample["robot_mode"]), "safety_mode": int(sample["safety_mode"]),
        "actual_TCP_pose": list(actual), "actual_TCP_speed": list(actual_speed), "actual_qd": list(qd),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    offline = sub.add_parser("offline")
    offline.add_argument("--duration", choices=[f"{value}s" for value in DURATION_OPTIONS_S], required=True)
    offline.add_argument("--output-dir", type=Path, required=True)
    offline.add_argument("--startup-safe-z", type=float, default=0.029423891)
    package = sub.add_parser("package")
    package.add_argument("--output-dir", type=Path, required=True)
    status = sub.add_parser("status")
    status.add_argument("--robot-host", default=ROBOT_HOST_DEFAULT)
    live = sub.add_parser("run")
    live.add_argument("--duration", choices=[f"{value}s" for value in DURATION_OPTIONS_S], required=True)
    live.add_argument("--robot-host", default=ROBOT_HOST_DEFAULT)
    live.add_argument("--sensor-ip", default=SENSOR_HOST_DEFAULT)
    live.add_argument("--sensor-port", type=int, default=SENSOR_PORT_DEFAULT)
    live.add_argument("--output-dir", type=Path, required=True)
    live.add_argument("--connect-timeout-s", type=float, default=3.0)
    live.add_argument("--preposition-timeout-s", type=float, default=20.0)
    live.add_argument("--return-timeout-s", type=float, default=20.0)
    live.add_argument("--kunwei-watchdog-s", type=float, default=0.080)
    live.add_argument("--kunwei-start-timeout-s", type=float, default=3.0)
    live.add_argument("--rtde-watchdog-s", type=float, default=0.100)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "offline":
            args.output_dir.mkdir(parents=True, exist_ok=False)
            payload = build_offline_evidence(args.duration, startup_safe_z_m=args.startup_safe_z)
            _write_json(args.output_dir / "evidence.json", payload)
            print(json.dumps({"ok": True, "profile_id": PROFILE_ID, "duration_s": payload["duration_s"], "sample_count": payload["sample_count"], "output": str(args.output_dir)}, indent=2))
        elif args.command == "package":
            print(json.dumps(build_package(args.output_dir), indent=2, sort_keys=True))
        elif args.command == "status":
            print(json.dumps(readonly_status(args.robot_host), indent=2, sort_keys=True))
        else:
            print(json.dumps(run_live(args), indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
