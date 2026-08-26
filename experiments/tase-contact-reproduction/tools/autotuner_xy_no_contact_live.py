#!/usr/bin/env python3
"""Live owner for the dedicated one-shot autotuner XY no-contact diagnostic."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import statistics
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

import numpy as np

import step5d_autotune_v4_live_writer as canonical_v4
from build_autotuner_xy_no_contact_package import CONTROLLER_DIR, LAYOUT_TAG, PROGRAM
from step5c_strict_rnn import StrictRnnConfig, StrictTaseRnnSolver
from step5d_autotune_v3.rtde_client import dashboard_exchange
from step5d_autotune_v4_r004.calibrated_runtime import tcp_jacobian_base
from step5c_calibrated_kinematics_audit import build_calibrated_model
from step5d_paper_outer_loop import rotvec_to_matrix


ROOT = Path(__file__).resolve().parents[1]
ROBOT_HOST = "192.168.1.18"
KUNWEI_HOST = "192.168.50.25"
KUNWEI_PORT = 5152
RTDE_HZ = 500.0
PERIOD_S = 1.0 / RTDE_HZ
OUTPUT_FIELDS = (
    "timestamp",
    "payload",
    "payload_cog",
    "tcp_offset",
    "actual_TCP_speed",
    "actual_TCP_pose",
    "actual_q",
    "actual_qd",
    "safety_mode",
    "robot_mode",
    "runtime_state",
    "output_double_register_26",
    "output_double_register_28",
    "output_double_register_35",
    "output_double_register_36",
    "output_double_register_37",
    "output_double_register_38",
    "output_double_register_39",
    "output_double_register_40",
    "output_double_register_41",
    "output_double_register_42",
    "output_double_register_43",
    "output_double_register_44",
    "output_double_register_45",
    "output_double_register_46",
    "output_double_register_47",
)
INPUT_FIELDS = tuple(f"input_double_register_{index}" for index in range(24, 48))
CSV_FIELDS = (
    "sample_index",
    "host_monotonic_s",
    "controller_timestamp_s",
    "stage",
    "path_time_s",
    "desired_base_x_m",
    "desired_base_y_m",
    "actual_base_x_m",
    "actual_base_y_m",
    "desired_local_along_m",
    "desired_local_lateral_m",
    "actual_local_along_m",
    "actual_local_lateral_m",
    "desired_z_m",
    "actual_z_m",
    "actual_rx_rad",
    "actual_ry_rad",
    "actual_rz_rad",
    "desired_vx_m_s",
    "desired_vy_m_s",
    "actual_vx_m_s",
    "actual_vy_m_s",
    "actual_vz_m_s",
    "path_error_along_m",
    "path_error_lateral_m",
    "path_error_norm_m",
    "hard_ellipse_value",
    "soft_cbf_qp_active",
    "force_norm_n",
    "torque_norm_nm",
    "fixed_z_error_m",
    "orientation_error_rad",
    "command_speed_m_s",
    "qdot_max_abs_rad_s",
    "qdot_json",
    "heartbeat",
    "sensor_age_s",
    "controller_consumed",
    "solver_residual_norm",
    "fault_reason",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dashboard(commands: Sequence[str]) -> dict[str, str]:
    return dashboard_exchange(ROBOT_HOST, commands, timeout=3.0)


def _loaded_path(response: str) -> str | None:
    text = response.strip()
    if not text.lower().startswith("loaded program:"):
        return None
    value = text.split(":", 1)[1].strip()
    return value if value and value.lower() != "no program loaded" else None


def _known_live_writer_processes() -> list[str]:
    patterns = (
        "kunwei_rtde_bridge.py",
        "step5d_autotune_v4_live_writer.py",
        "step5d_autotune_v4_r004_live_writer.py",
        "run_step5d_autotune_v3_live.py",
    )
    found: list[str] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if command and any(pattern in command for pattern in patterns):
            found.append(f"{entry.name}:{command.strip()}")
    return found


class DiagnosticRTDE:
    def __init__(self) -> None:
        self.client: canonical_v4.WritableRTDEClient | None = None
        self.output_recipe = 0
        self.output_types: list[str] = []
        self.input_recipe = 0
        self.input_types: list[str] = []
        self.latest: dict[str, Any] = {}

    def open(self) -> None:
        client = canonical_v4.WritableRTDEClient(ROBOT_HOST, timeout=3.0)
        client.__enter__()
        try:
            client.negotiate()
            self.output_recipe, self.output_types = client.setup_outputs(RTDE_HZ, OUTPUT_FIELDS)
            self.input_recipe, self.input_types = client.setup_inputs(INPUT_FIELDS)
            if any(value != "DOUBLE" for value in self.input_types):
                raise RuntimeError("diagnostic input recipe is not DOUBLE-only")
            client.start()
        except Exception:
            client.__exit__(None, None, None)
            raise
        self.client = client

    def close(self) -> None:
        client = self.client
        self.client = None
        if client is not None:
            client.__exit__(None, None, None)

    def poll(self) -> dict[str, Any]:
        client = self.client
        if client is None:
            raise RuntimeError("RTDE is not open")
        observed = client.recv_latest_sample(self.output_recipe, self.output_types, OUTPUT_FIELDS)
        if observed is not None:
            self.latest = observed
        return self.latest

    def send(self, values: Mapping[int, float]) -> None:
        client = self.client
        if client is None:
            raise RuntimeError("RTDE is not open")
        packet = [float(values.get(index, 0.0)) for index in range(24, 48)]
        if not all(math.isfinite(value) for value in packet):
            raise RuntimeError("nonfinite RTDE input packet")
        client.send_input_sample(self.input_recipe, self.input_types, packet)


def _fresh_output(rtde: DiagnosticRTDE, timeout_s: float = 3.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = rtde.poll()
        if value:
            return value
        time.sleep(0.002)
    raise RuntimeError("fresh RTDE output timeout")


def _stationary(output: Mapping[str, Any]) -> bool:
    speed = tuple(float(value) for value in output["actual_TCP_speed"])
    qd = tuple(float(value) for value in output["actual_qd"])
    return (
        math.sqrt(sum(value * value for value in speed[:3])) <= 0.0005
        and math.sqrt(sum(value * value for value in speed[3:])) <= 0.005
        and max(abs(value) for value in qd) <= 0.01
    )


def _orientation_velocity(actual_rotvec: Sequence[float], anchor_rotvec: Sequence[float]) -> np.ndarray:
    current = rotvec_to_matrix(np.asarray(actual_rotvec, dtype=float))
    anchor = rotvec_to_matrix(np.asarray(anchor_rotvec, dtype=float))
    error = anchor @ current.T
    vee = 0.5 * np.asarray(
        (error[2, 1] - error[1, 2], error[0, 2] - error[2, 0], error[1, 0] - error[0, 1]),
        dtype=float,
    )
    return 0.01 * vee


def _local(api: Any, xy: Sequence[float]) -> tuple[float, float]:
    dx = float(xy[0]) - api.BASE_ORIGIN_XY_M[0]
    dy = float(xy[1]) - api.BASE_ORIGIN_XY_M[1]
    return (
        dx * api.U_ALONG_XY[0] + dy * api.U_ALONG_XY[1],
        dx * api.P_LATERAL_XY[0] + dy * api.P_LATERAL_XY[1],
    )


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"path_rows": 0}
    desired = np.asarray([[float(row["desired_base_x_m"]), float(row["desired_base_y_m"])] for row in rows])
    actual = np.asarray([[float(row["actual_base_x_m"]), float(row["actual_base_y_m"])] for row in rows])
    errors = np.linalg.norm(actual - desired, axis=1)
    desired_velocity = np.asarray([[float(row["desired_vx_m_s"]), float(row["desired_vy_m_s"])] for row in rows])
    actual_velocity = np.asarray([[float(row["actual_vx_m_s"]), float(row["actual_vy_m_s"])] for row in rows])
    desired_speed = np.linalg.norm(desired_velocity, axis=1)
    actual_speed = np.linalg.norm(actual_velocity, axis=1)
    corr = None
    if np.std(desired_speed) > 1e-12 and np.std(actual_speed) > 1e-12:
        corr = float(np.corrcoef(desired_speed, actual_speed)[0, 1])
    max_lag = min(1000, max(0, len(rows) // 10))
    best_lag = 0
    best_corr = -math.inf
    if len(rows) >= 20:
        for lag in range(-max_lag, max_lag + 1):
            if lag < 0:
                left, right = desired_speed[-lag:], actual_speed[:lag]
            elif lag > 0:
                left, right = desired_speed[:-lag], actual_speed[lag:]
            else:
                left, right = desired_speed, actual_speed
            if len(left) >= 10 and np.std(left) > 1e-12 and np.std(right) > 1e-12:
                value = float(np.corrcoef(left, right)[0, 1])
                if value > best_corr:
                    best_corr, best_lag = value, lag
    raw_actual_steps = np.linalg.norm(np.diff(actual, axis=0), axis=1)
    raw_desired_steps = np.linalg.norm(np.diff(desired, axis=0), axis=1)
    # At 500 Hz, micrometre-scale RTDE pose quantization/jitter dominates a
    # 3 mm/s reference's 6 um nominal step and grossly inflates raw polyline
    # length.  Report that raw diagnostic explicitly, but use frozen 50 ms
    # block means for the physically meaningful 20 Hz path ratios.
    path_ratio_window_samples = 25
    blocks = len(rows) // path_ratio_window_samples
    filtered_actual = actual[: blocks * path_ratio_window_samples].reshape(
        blocks, path_ratio_window_samples, 2
    ).mean(axis=1)
    filtered_desired = desired[: blocks * path_ratio_window_samples].reshape(
        blocks, path_ratio_window_samples, 2
    ).mean(axis=1)
    actual_steps = np.linalg.norm(np.diff(filtered_actual, axis=0), axis=1)
    desired_steps = np.linalg.norm(np.diff(filtered_desired, axis=0), axis=1)
    tail_start = max(0, int(len(actual_steps) * 0.9))
    raw_tail_start = max(0, int(len(raw_actual_steps) * 0.9))
    duration = float(rows[-1]["path_time_s"]) - float(rows[0]["path_time_s"])
    return {
        "path_rows": len(rows),
        "active_duration_s": duration,
        "effective_sample_rate_hz": (len(rows) - 1) / duration if duration > 0.0 else None,
        "endpoint_error_m": float(errors[-1]),
        "peak_error_m": float(np.max(errors)),
        "rmse_m": float(np.sqrt(np.mean(errors * errors))),
        "overall_path_ratio": float(np.sum(actual_steps) / max(np.sum(desired_steps), 1e-12)),
        "tail_path_ratio": float(np.sum(actual_steps[tail_start:]) / max(np.sum(desired_steps[tail_start:]), 1e-12)),
        "path_ratio_filter": {
            "method": "nonoverlapping_block_mean",
            "window_samples": path_ratio_window_samples,
            "window_s": path_ratio_window_samples * PERIOD_S,
            "output_rate_hz": 1.0 / (path_ratio_window_samples * PERIOD_S),
        },
        "raw_500hz_overall_path_ratio_noise_sensitive": float(
            np.sum(raw_actual_steps) / max(np.sum(raw_desired_steps), 1e-12)
        ),
        "raw_500hz_tail_path_ratio_noise_sensitive": float(
            np.sum(raw_actual_steps[raw_tail_start:])
            / max(np.sum(raw_desired_steps[raw_tail_start:]), 1e-12)
        ),
        "velocity_correlation": corr,
        "best_velocity_correlation": None if best_corr == -math.inf else best_corr,
        "best_lag_samples": best_lag,
        "best_lag_s": best_lag * PERIOD_S,
        "actual_xy_span_m": [float(np.ptp(actual[:, 0])), float(np.ptp(actual[:, 1]))],
        "desired_xy_span_m": [float(np.ptp(desired[:, 0])), float(np.ptp(desired[:, 1]))],
    }


def _plots(rows: list[dict[str, Any]], output_dir: Path) -> dict[str, str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if not rows:
        return {}
    t = np.asarray([float(row["path_time_s"]) for row in rows])
    dx = np.asarray([float(row["desired_base_x_m"]) for row in rows])
    dy = np.asarray([float(row["desired_base_y_m"]) for row in rows])
    ax = np.asarray([float(row["actual_base_x_m"]) for row in rows])
    ay = np.asarray([float(row["actual_base_y_m"]) for row in rows])
    dz = np.asarray([float(row["desired_z_m"]) for row in rows])
    az = np.asarray([float(row["actual_z_m"]) for row in rows])
    error = np.hypot(ax - dx, ay - dy)
    files: dict[str, str] = {}

    fig, axis = plt.subplots(figsize=(7, 6))
    axis.plot(ax * 1000, ay * 1000, linewidth=1.0, color="#0f766e")
    axis.scatter([ax[0] * 1000], [ay[0] * 1000], s=30, label="start")
    axis.scatter([ax[-1] * 1000], [ay[-1] * 1000], s=30, label="end")
    axis.set_aspect("equal", adjustable="box")
    axis.set_xlabel("base X [mm]")
    axis.set_ylabel("base Y [mm]")
    axis.set_title("Actual-only XY (equal aspect)")
    axis.grid(True, alpha=0.25)
    axis.legend()
    path = output_dir / "actual_only_xy.png"
    fig.tight_layout(); fig.savefig(path, dpi=180); plt.close(fig)
    files["actual_only_xy"] = path.name

    fig, axis = plt.subplots(figsize=(7, 6))
    axis.plot(dx * 1000, dy * 1000, label="desired", linewidth=1.2)
    axis.plot(ax * 1000, ay * 1000, label="actual", linewidth=1.0)
    axis.set_aspect("equal", adjustable="box")
    axis.set_xlabel("base X [mm]"); axis.set_ylabel("base Y [mm]")
    axis.set_title("Desired vs actual XY (equal aspect)")
    axis.grid(True, alpha=0.25); axis.legend()
    path = output_dir / "desired_vs_actual_xy.png"
    fig.tight_layout(); fig.savefig(path, dpi=180); plt.close(fig)
    files["desired_vs_actual_xy"] = path.name

    fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True)
    axes[0].plot(t, dx * 1000, label="desired"); axes[0].plot(t, ax * 1000, label="actual")
    axes[1].plot(t, dy * 1000, label="desired"); axes[1].plot(t, ay * 1000, label="actual")
    axes[2].plot(t, dz * 1000, label="desired"); axes[2].plot(t, az * 1000, label="actual")
    for label, axis in zip(("X [mm]", "Y [mm]", "Z [mm]"), axes):
        axis.set_ylabel(label); axis.grid(True, alpha=0.25); axis.legend(loc="best")
    axes[-1].set_xlabel("PATH time [s]")
    path = output_dir / "xyz_vs_time.png"
    fig.tight_layout(); fig.savefig(path, dpi=180); plt.close(fig)
    files["xyz_vs_time"] = path.name

    desired_speed = np.hypot(
        np.asarray([float(row["desired_vx_m_s"]) for row in rows]),
        np.asarray([float(row["desired_vy_m_s"]) for row in rows]),
    )
    actual_speed = np.hypot(
        np.asarray([float(row["actual_vx_m_s"]) for row in rows]),
        np.asarray([float(row["actual_vy_m_s"]) for row in rows]),
    )
    fig, axes = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
    axes[0].plot(t, error * 1000); axes[0].set_ylabel("XY error [mm]"); axes[0].grid(True, alpha=0.25)
    axes[1].plot(t, desired_speed * 1000, label="desired"); axes[1].plot(t, actual_speed * 1000, label="actual")
    axes[1].set_ylabel("XY speed [mm/s]"); axes[1].set_xlabel("PATH time [s]"); axes[1].grid(True, alpha=0.25); axes[1].legend()
    path = output_dir / "path_error_velocity_lag.png"
    fig.tight_layout(); fig.savefig(path, dpi=180); plt.close(fig)
    files["path_error_velocity_lag"] = path.name
    return files


def run_live(
    api: Any,
    *,
    output_dir: Path,
    package_manifest: Path,
    readback_manifest: Path,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=False)
    package = json.loads(package_manifest.read_text(encoding="utf-8"))
    readback = json.loads(readback_manifest.read_text(encoding="utf-8"))
    if package.get("program") != PROGRAM or readback.get("fresh_controller_sha_verified") is not True:
        raise RuntimeError("dedicated controller package/read-back identity is not verified")
    active_writers = _known_live_writer_processes()
    if active_writers:
        raise RuntimeError(f"single-live-writer preflight failed: {active_writers}")

    dashboard_before = _dashboard(["robotmode", "safetystatus", "programState", "get loaded program", "is in remote control"])
    previous_loaded = _loaded_path(dashboard_before.get("get loaded program", ""))
    if "NORMAL" not in dashboard_before.get("safetystatus", "").upper():
        raise RuntimeError(f"Safety is not NORMAL: {dashboard_before}")
    if "TRUE" not in dashboard_before.get("is in remote control", "").upper():
        raise RuntimeError(f"controller is not in Remote Control: {dashboard_before}")
    if "STOPPED" not in dashboard_before.get("programState", "").upper():
        raise RuntimeError(f"controller program is not stopped: {dashboard_before}")

    rtde = DiagnosticRTDE()
    kunwei = canonical_v4.LiveKunweiTransport(
        SimpleNamespace(sensor_host=KUNWEI_HOST, sensor_port=KUNWEI_PORT)
    )
    raw_path = output_dir / "kunwei_raw_frames.bin"
    csv_path = output_dir / "rtde_500hz.csv"
    terminal_fault: str | None = None
    normal_complete = False
    restored = False
    path_rows: list[dict[str, Any]] = []
    all_rows = 0
    force_peak = 0.0
    torque_peak = 0.0
    baseline_rows: list[tuple[float, float, float, float, float, float]] = []
    baseline: tuple[float, float, float, float, float, float] | None = None
    latest_output: dict[str, Any] = {}
    latest_wrench = (0.0,) * 6
    heartbeat = 0.0
    path_start: float | None = None
    last_path_time = 0.0
    last_tick = time.monotonic()
    previous_qdot = np.zeros(6, dtype=float)
    solver: StrictTaseRnnSolver | None = None
    model = build_calibrated_model()
    guard_stack = api.R012PathGuardStack()
    home_pose: Any = None
    path_origin_pose: Any = None
    first_controller_timestamp: float | None = None
    abort_code = 0.0
    phase_command = 0.0
    sensor_fresh = False
    saw_dedicated_stage = False

    def packet(qdot: Sequence[float] = (0.0,) * 6, *, valid: bool = False, path_time: float = 0.0) -> dict[int, float]:
        values = {
            24: force_peak if not sensor_fresh else math.sqrt(sum(value * value for value in latest_wrench[:3])),
            25: torque_peak if not sensor_fresh else math.sqrt(sum(value * value for value in latest_wrench[3:])),
            26: heartbeat,
            27: 1.0 if sensor_fresh else 0.0,
            28: abort_code,
            29: phase_command,
            43: 1.0 if valid else 0.0,
            44: path_time,
            47: LAYOUT_TAG if valid else 0.0,
        }
        for offset, value in enumerate(qdot, start=37):
            values[offset] = float(value)
        return values

    try:
        kunwei.open()
        rtde.open()
        latest_output = _fresh_output(rtde)
        if latest_output.get("safety_mode") != 1 or latest_output.get("robot_mode") != 7:
            raise RuntimeError("fresh RTDE Safety/robot mode preflight failed")
        if not _stationary(latest_output):
            raise RuntimeError("robot is not stationary before diagnostic")
        home_pose = api.Pose.from_sequence(latest_output["actual_TCP_pose"])
        path_origin_pose = api.Pose(
            api.BASE_ORIGIN_XY_M[0], api.BASE_ORIGIN_XY_M[1], home_pose.z_m,
            home_pose.rx_rad, home_pose.ry_rad, home_pose.rz_rad,
        )
        with raw_path.open("wb") as raw_handle:
            kunwei.attach_raw_sink(raw_handle)
            baseline_deadline = time.monotonic() + 3.0
            while time.monotonic() < baseline_deadline and len(baseline_rows) < 1000:
                frames, _ = kunwei.poll()
                for frame in frames:
                    baseline_rows.append(canonical_v4.zeroed_wrench(frame, (0.0,) * 6))
                time.sleep(0.001)
            if len(baseline_rows) < 1000:
                raise RuntimeError(f"Kunwei baseline has only {len(baseline_rows)} samples")
            baseline = tuple(statistics.fmean(axis) for axis in zip(*baseline_rows, strict=True))
            sensor_fresh = True
            load_target = f"{CONTROLLER_DIR}/{PROGRAM}.urp"
            # Dashboard Load invalidates the controller-side RTDE session on
            # this bench.  Close the stopped-program observation connection,
            # Load, then establish and re-verify the sole writer connection.
            rtde.close()
            dashboard_load = _dashboard([f"load {load_target}"])
            if "loading program" not in dashboard_load[f"load {load_target}"].lower():
                raise RuntimeError(f"dedicated controller load failed: {dashboard_load}")
            rtde.open()
            latest_output = _fresh_output(rtde)
            if latest_output.get("safety_mode") != 1 or latest_output.get("robot_mode") != 7:
                raise RuntimeError("post-Load fresh RTDE Safety/robot mode verification failed")
            if not _stationary(latest_output):
                raise RuntimeError("robot is not stationary after dedicated package Load")

            # Keep the single RTDE writer live before Play so the TP sees a
            # fresh sensor/heartbeat immediately at its entry wait.
            for _ in range(10):
                heartbeat += 1.0
                rtde.send(packet())
                kunwei.poll()
                time.sleep(PERIOD_S)
            dashboard_play = _dashboard(["play"])
            if "starting program" not in dashboard_play["play"].lower():
                raise RuntimeError(f"dedicated controller Play failed: {dashboard_play}")

            solver = StrictTaseRnnSolver(
                StrictRnnConfig(
                    paper_truth_path=ROOT / "config" / "step5d_liveprep_solver_gate.json",
                    qdot_limit_rad_s=0.15,
                    epsilon=0.022,
                    sigr_exponent_r=1.0,
                    inner_iterations=1,
                    backend="numpy",
                )
            )
            with csv_path.open("w", newline="", encoding="utf-8") as csv_handle:
                writer = csv.DictWriter(csv_handle, fieldnames=CSV_FIELDS)
                writer.writeheader()
                run_start = time.monotonic()
                next_deadline = run_start
                last_sensor_mono: float | None = None
                while time.monotonic() - run_start < 170.0:
                    now = time.monotonic()
                    if now < next_deadline:
                        time.sleep(next_deadline - now)
                    now = time.monotonic()
                    while next_deadline <= now:
                        next_deadline += PERIOD_S
                    dt = min(max(now - last_tick, 1e-5), 0.079)
                    last_tick = now
                    frames, _ = kunwei.poll()
                    if frames:
                        assert baseline is not None
                        latest_wrench = canonical_v4.zeroed_wrench(frames[-1], baseline)
                        last_sensor_mono = float(kunwei.latest_mono)
                    sensor_age = math.inf if last_sensor_mono is None else now - last_sensor_mono
                    sensor_fresh = sensor_age < 0.05
                    force_norm = math.sqrt(sum(value * value for value in latest_wrench[:3]))
                    torque_norm = math.sqrt(sum(value * value for value in latest_wrench[3:]))
                    force_peak = max(force_peak, force_norm)
                    torque_peak = max(torque_peak, torque_norm)
                    heartbeat += 1.0
                    latest_output = rtde.poll()
                    if not latest_output:
                        abort_code = 20.0
                        terminal_fault = "rtde_output_missing"
                    stage = float(latest_output.get("output_double_register_35", 0.0))
                    stop_reason = float(latest_output.get("output_double_register_28", 0.0))
                    if any(abs(stage - value) < 0.05 for value in (20.0, 30.0, 25.0, 40.0, 41.0)):
                        saw_dedicated_stage = True
                    pose_values = latest_output.get("actual_TCP_pose", home_pose.as_tuple())
                    speed_values = latest_output.get("actual_TCP_speed", (0.0,) * 6)
                    q_values = latest_output.get("actual_q", (0.0,) * 6)
                    qd_values = latest_output.get("actual_qd", (0.0,) * 6)
                    actual_pose = api.Pose.from_sequence(pose_values)
                    if latest_output.get("safety_mode") != 1 or latest_output.get("robot_mode") != 7:
                        abort_code = 21.0
                        terminal_fault = "rtde_safety_or_robot_mode"
                    elif not sensor_fresh:
                        abort_code = 2.0
                        terminal_fault = "kunwei_stale"
                    elif force_norm >= 6.0:
                        abort_code = 6.0
                        terminal_fault = "force_limit_exceeded"
                    elif torque_norm >= 0.5:
                        abort_code = 7.0
                        terminal_fault = "torque_limit_exceeded"
                    z_error = abs(actual_pose.z_m - home_pose.z_m)
                    orientation_error = math.sqrt(sum((a-b)**2 for a,b in zip(actual_pose.as_tuple()[3:], home_pose.as_tuple()[3:])))
                    if z_error > 0.002 and terminal_fault is None:
                        abort_code = 8.0; terminal_fault = "fixed_z_error_exceeded"
                    if orientation_error > math.radians(5.0) and terminal_fault is None:
                        abort_code = 9.0; terminal_fault = "orientation_limit_exceeded"

                    qdot = np.zeros(6, dtype=float)
                    command_speed = 0.0
                    guarded = None
                    residual = math.nan
                    desired = api.trajectory_at(0.0, path_origin_pose)
                    if abs(stage - 25.0) < 0.05 and terminal_fault is None:
                        if path_start is None:
                            path_start = now
                            first_controller_timestamp = float(latest_output["timestamp"])
                            solver.reset_state()
                        path_time = min(max(now - path_start, 0.0), 60.0)
                        last_path_time = path_time
                        desired = api.trajectory_at(path_time, path_origin_pose)
                        error = np.asarray(
                            (desired.desired_pose.x_m-actual_pose.x_m, desired.desired_pose.y_m-actual_pose.y_m, home_pose.z_m-actual_pose.z_m),
                            dtype=float,
                        )
                        nominal = np.zeros(6, dtype=float)
                        nominal[:2] = np.asarray(desired.desired_velocity_xy_m_s) + error[:2]
                        nominal[2] = error[2]
                        nominal[3:] = _orientation_velocity(actual_pose.as_tuple()[3:], home_pose.as_tuple()[3:])
                        guarded = api.guard_path_command(
                            desired,
                            actual_pose,
                            guard_stack=guard_stack,
                            nominal_twist=nominal,
                        )
                        command_twist = np.asarray(guarded.command_twist, dtype=float)
                        linear_norm = float(np.linalg.norm(command_twist[:3]))
                        if linear_norm > 0.004:
                            command_twist[:3] *= 0.004 / linear_norm
                        command_speed = float(np.linalg.norm(command_twist[:3]))
                        if guarded.terminal_stop:
                            abort_code = 10.0; terminal_fault = guarded.reason or "moving_reference_hard_ellipse"
                        else:
                            jacobian = tcp_jacobian_base(model, q_values)
                            lower = np.maximum(model.model.lowerPositionLimit - np.asarray(q_values), -0.15)
                            upper = np.minimum(model.model.upperPositionLimit - np.asarray(q_values), 0.15)
                            if not path_rows:
                                solver.warm_start(J=jacobian, xdot_c=command_twist, omega_minus=lower, omega_plus=upper)
                            result = solver.solve(
                                actual_q=q_values,
                                actual_qd=qd_values,
                                target_state={
                                    "J": jacobian, "xdot_c": command_twist,
                                    "omega_minus": lower, "omega_plus": upper,
                                    "dt": dt, "epsilon": 0.022, "r": 1.0, "cmd_valid": True,
                                },
                            )
                            residual = float(result.residual_norm)
                            candidate = np.asarray(result.qdot, dtype=float)
                            delta = np.clip(candidate - previous_qdot, -0.05*dt, 0.05*dt)
                            qdot = np.clip(previous_qdot + delta, -0.15, 0.15)
                            predicted = jacobian @ qdot
                            predicted_linear = float(np.linalg.norm(predicted[:3]))
                            if predicted_linear > 0.004:
                                qdot *= 0.004 / predicted_linear
                            previous_qdot = qdot.copy()
                        if path_time >= 60.0:
                            phase_command = 2.0
                    else:
                        previous_qdot[:] = 0.0

                    heartbeat += 1.0
                    rtde.send(packet(qdot, valid=abs(stage-25.0)<0.05 and terminal_fault is None, path_time=last_path_time))
                    if abs(stage - 25.0) < 0.05:
                        desired_local = (desired.local_along_m, desired.local_lateral_m)
                        actual_local = _local(api, (actual_pose.x_m, actual_pose.y_m))
                        row = {
                            "sample_index": len(path_rows),
                            "host_monotonic_s": now,
                            "controller_timestamp_s": float(latest_output.get("timestamp", math.nan)),
                            "stage": stage,
                            "path_time_s": last_path_time,
                            "desired_base_x_m": desired.desired_pose.x_m,
                            "desired_base_y_m": desired.desired_pose.y_m,
                            "actual_base_x_m": actual_pose.x_m,
                            "actual_base_y_m": actual_pose.y_m,
                            "desired_local_along_m": desired_local[0],
                            "desired_local_lateral_m": desired_local[1],
                            "actual_local_along_m": actual_local[0],
                            "actual_local_lateral_m": actual_local[1],
                            "desired_z_m": home_pose.z_m,
                            "actual_z_m": actual_pose.z_m,
                            "actual_rx_rad": actual_pose.rx_rad,
                            "actual_ry_rad": actual_pose.ry_rad,
                            "actual_rz_rad": actual_pose.rz_rad,
                            "desired_vx_m_s": desired.desired_velocity_xy_m_s[0],
                            "desired_vy_m_s": desired.desired_velocity_xy_m_s[1],
                            "actual_vx_m_s": float(speed_values[0]),
                            "actual_vy_m_s": float(speed_values[1]),
                            "actual_vz_m_s": float(speed_values[2]),
                            "path_error_along_m": actual_local[0]-desired_local[0],
                            "path_error_lateral_m": actual_local[1]-desired_local[1],
                            "path_error_norm_m": math.hypot(actual_local[0]-desired_local[0], actual_local[1]-desired_local[1]),
                            "hard_ellipse_value": None if guarded is None else guarded.hard_ellipse_value,
                            "soft_cbf_qp_active": False if guarded is None else guarded.soft_cbf_qp_active,
                            "force_norm_n": force_norm,
                            "torque_norm_nm": torque_norm,
                            "fixed_z_error_m": z_error,
                            "orientation_error_rad": orientation_error,
                            "command_speed_m_s": command_speed,
                            "qdot_max_abs_rad_s": float(np.max(np.abs(qdot))),
                            "qdot_json": json.dumps([float(value) for value in qdot]),
                            "heartbeat": heartbeat,
                            "sensor_age_s": sensor_age,
                            "controller_consumed": float(latest_output.get("output_double_register_47", 0.0)),
                            "solver_residual_norm": residual,
                            "fault_reason": terminal_fault or "",
                        }
                        writer.writerow(row); csv_handle.flush()
                        path_rows.append(row); all_rows += 1
                    if saw_dedicated_stage and (abs(stage - 90.0) < 0.05 or stop_reason != 0.0):
                        if terminal_fault is None:
                            terminal_fault = f"controller_stop_reason_{stop_reason:g}"
                        break
                    if saw_dedicated_stage and abs(stage - 78.0) < 0.05:
                        time.sleep(0.25)
                        latest_output = _fresh_output(rtde)
                        normal_complete = _stationary(latest_output)
                        if not normal_complete:
                            terminal_fault = "terminal_stationary_verification_fault"
                        break
                if not normal_complete and terminal_fault is None:
                    terminal_fault = "live_runtime_timeout"
                    abort_code = 22.0
                    heartbeat += 1.0
                    rtde.send(packet())
    finally:
        try:
            rtde.send({26: heartbeat + 1.0, 27: 0.0, 28: abort_code or (0.0 if normal_complete else 22.0)})
        except Exception:
            pass
        try:
            kunwei.close()
        finally:
            rtde.close()
        if previous_loaded:
            try:
                restored_response = _dashboard([f"load {previous_loaded}"])
                restored = "loading program" in restored_response[f"load {previous_loaded}"].lower()
            except Exception:
                restored = False

    metrics = _metrics(path_rows)
    plots = _plots(path_rows, output_dir)
    terminal_snapshot = {
        "dashboard": _dashboard(["robotmode", "safetystatus", "programState", "get loaded program"]),
        "rtde": {
            "safety_mode": latest_output.get("safety_mode"),
            "robot_mode": latest_output.get("robot_mode"),
            "runtime_state": latest_output.get("runtime_state"),
            "actual_TCP_pose": latest_output.get("actual_TCP_pose"),
            "actual_TCP_speed": latest_output.get("actual_TCP_speed"),
            "actual_qd": latest_output.get("actual_qd"),
        },
    }
    (output_dir / "terminal_safety_snapshot.json").write_text(json.dumps(terminal_snapshot, indent=2, sort_keys=True) + "\n")
    kunwei_summary = {
        "baseline_samples": len(baseline_rows),
        "force_peak_n": force_peak,
        "torque_peak_nm": torque_peak,
        "raw_file": raw_path.name,
    }
    (output_dir / "kunwei_summary.json").write_text(json.dumps(kunwei_summary, indent=2, sort_keys=True) + "\n")
    identity = {
        "program": PROGRAM,
        "controller_target": f"{CONTROLLER_DIR}/{PROGRAM}.urp",
        "package_manifest": str(package_manifest),
        "readback_manifest": str(readback_manifest),
        "package_sha256": package.get("sha256"),
        "fresh_controller_sha_verified": readback.get("fresh_controller_sha_verified"),
        "previous_loaded_program": previous_loaded,
        "previous_identity_restored_without_play": restored,
    }
    (output_dir / "controller_source_identity.json").write_text(json.dumps(identity, indent=2, sort_keys=True) + "\n")
    evidence = {
        "schema": api.SCHEMA,
        "claim_class": api.CLAIM_CLASS,
        "mode": "live_single_family_diagnostic_not_for_promotion",
        "activation": {
            "contact": False, "search": False, "force_control": False,
            "model": False, "candidate": False, "training_dataset": False,
            "qualification": False, "promotion": False, "tell_exact": False,
        },
        "trajectory": {
            "origin_xy_m": list(api.BASE_ORIGIN_XY_M),
            "u_along_xy": list(api.U_ALONG_XY),
            "p_lateral_xy": list(api.P_LATERAL_XY),
            "duration_s": 60.0,
            "session_home_pose": None if home_pose is None else list(home_pose.as_tuple()),
            "path_origin_pose": None if path_origin_pose is None else list(path_origin_pose.as_tuple()),
            "fixed_startup_z_orientation": True,
        },
        "safety": {
            "limits": api.FAIL_CLOSED_LIMITS.as_dict(),
            "guard_order": list(api.GUARD_ORDER),
            "single_live_writer_preflight": True,
            "automatic_recovery_motion": False,
        },
        "runtime": {
            "complete_60s_timeline": bool(metrics.get("active_duration_s", 0.0) >= 59.99),
            "normal_complete": normal_complete,
            "fault_reason": terminal_fault,
            "return_home_stationary": normal_complete,
            "path_rows": len(path_rows),
            "first_controller_timestamp": first_controller_timestamp,
        },
        "tracking_metrics_non_promotional": metrics,
        "controller_source_identity": identity,
        "kunwei_summary": kunwei_summary,
        "plots": plots,
        "evidence_files": {
            "rtde_500hz_csv": csv_path.name,
            "kunwei_raw": raw_path.name,
            "kunwei_summary": "kunwei_summary.json",
            "controller_source_identity": "controller_source_identity.json",
            "terminal_safety_snapshot": "terminal_safety_snapshot.json",
        },
    }
    (output_dir / "evidence.json").write_text(json.dumps(evidence, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return evidence


__all__ = ["run_live"]
