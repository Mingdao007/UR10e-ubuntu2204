from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pinocchio as pin
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import JointState
from std_msgs.msg import Float64MultiArray

from . import ros2_controller_switching
from . import step5b_contact_control_core as core
from . import step5b_contact_live_runner as contact_runner
from .kunwei_persistent_monitor import KunweiMonitorConfig, KunweiPersistentMonitor
from .no_contact_motion_probe import dashboard_exchange
from .step5a_cartesian_cycloid_motion import (
    DEFAULT_CALIBRATION_YAML,
    DEFAULT_XACRO_PATH,
    EXPECTED_CALIBRATION_HASH,
    JOINT_NAMES,
    CalibratedModel,
    build_calibrated_model,
    fk_tool0_base,
)


WORKSPACE = Path(__file__).resolve().parents[3]
EXPERIMENT = WORKSPACE / "experiments" / "tase-contact-reproduction"
RUN_ROOT = EXPERIMENT / "runs"
LOCKED_ROUTE = "ros2_remote_control_headless"
ENTRYPOINT_NAME = "step5b_velocity_admittance_runner"
DEFAULT_COMMAND_TOPIC = "/forward_velocity_controller/commands"
CONTACT_CONTROL_STRATEGY = "forward_velocity_admittance"


@dataclass(frozen=True)
class VelocityStopResult:
    ok: bool
    exit_code: int
    failure_reason: str


def acceptance_contract(entrypoint: Path | None = None) -> dict[str, Any]:
    entrypoint_path = entrypoint or Path(__file__).resolve()
    return {
        "version": 1,
        "role": "step5b_live_runner_acceptance_candidate",
        "live_runner_entrypoint": str(entrypoint_path),
        "live_runner_console_script": ENTRYPOINT_NAME,
        "live_runner_route": LOCKED_ROUTE,
        "contact_control_strategy": CONTACT_CONTROL_STRATEGY,
        "velocity_command_topic": DEFAULT_COMMAND_TOPIC,
        "velocity_command_type": "std_msgs/msg/Float64MultiArray",
        "velocity_controller": ros2_controller_switching.FORWARD_VELOCITY_CONTROLLER,
        "joint_order": JOINT_NAMES,
        "force_source": "kunwei_software_baselined_stream",
        "zero_policy": {
            "ur_zero_ftsensor_called": False,
            "kunwei_hardware_tare_or_config_written": False,
            "software_baseline_subtraction": True,
        },
        "safety_boundary": [
            "default mode sends no velocity command",
            "no TP Play/upload",
            "no bridge start",
            "no URScript send",
            "no RTDE input writes",
            "no zero_ftsensor",
            "no Kunwei tare/config/write",
            "live command path requires step5b_authorization_status.py authorized",
            "live command path requires explicit --execute-live-contact",
        ],
    }


def damped_least_squares_qdot(
    jacobian: np.ndarray,
    twist_base: tuple[float, float, float, float, float, float],
    *,
    damping: float,
    max_joint_velocity_rad_s: float,
) -> np.ndarray:
    jac = np.array(jacobian, dtype=float)
    twist = np.array(twist_base, dtype=float)
    lhs = jac @ jac.T + (float(damping) ** 2) * np.eye(jac.shape[0])
    try:
        qdot = jac.T @ np.linalg.solve(lhs, twist)
    except np.linalg.LinAlgError:
        qdot = jac.T @ (np.linalg.pinv(lhs, rcond=1e-6) @ twist)
    limit = abs(float(max_joint_velocity_rad_s))
    if limit > 0.0:
        qdot = np.clip(qdot, -limit, limit)
    return qdot


def publish_velocity_command(publisher: Any, qdot: list[float] | np.ndarray | tuple[float, ...]) -> Float64MultiArray:
    values = [float(value) for value in qdot]
    if len(values) != len(JOINT_NAMES):
        raise ValueError(f"velocity command must contain {len(JOINT_NAMES)} joints, got {len(values)}")
    msg = Float64MultiArray()
    msg.data = values
    publisher.publish(msg)
    return msg


def publish_zero_velocity(publisher: Any) -> Float64MultiArray:
    return publish_velocity_command(publisher, [0.0] * len(JOINT_NAMES))


def assert_monitor_ready_or_stop(
    monitor: Any,
    publisher: Any,
    *,
    reason_prefix: str,
) -> VelocityStopResult:
    try:
        monitor.assert_fresh_and_within_force_delta()
    except RuntimeError as exc:
        publish_zero_velocity(publisher)
        return VelocityStopResult(False, 2, f"{reason_prefix}: {exc}")
    return VelocityStopResult(True, 0, "")


def velocity_trace_summary(rows: list[dict[str, Any]], *, command_period_s: float) -> dict[str, Any]:
    loop_dts = [float(row["loop_dt_s"]) for row in rows if "loop_dt_s" in row]
    jitters = [abs(value - float(command_period_s)) for value in loop_dts]
    elapsed_s = sum(loop_dts)
    return {
        "contact_control_strategy": CONTACT_CONTROL_STRATEGY,
        "intended_command_period_s": float(command_period_s),
        "target_command_rate_hz": 1.0 / float(command_period_s) if command_period_s > 0.0 else None,
        "actual_loop_rate_hz": (len(loop_dts) / elapsed_s) if elapsed_s > 0.0 else 0.0,
        "loop_count": len(loop_dts),
        "published_velocity_command_count": sum(1 for row in rows if bool(row.get("published"))),
        "velocity_command_sent": any(bool(row.get("published")) for row in rows),
        "loop_jitter_p50_s": _percentile(jitters, 50.0),
        "loop_jitter_p95_s": _percentile(jitters, 95.0),
        "loop_jitter_p99_s": _percentile(jitters, 99.0),
    }


def measure_no_motion_rate(*, command_period_s: float, duration_s: float) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    deadline = time.perf_counter() + max(0.0, duration_s)
    previous = time.perf_counter()
    while time.perf_counter() < deadline:
        time.sleep(max(0.0, command_period_s))
        now = time.perf_counter()
        rows.append({"loop_dt_s": now - previous, "published": False})
        previous = now
    return {
        "ok": True,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "role": "step5b_velocity_admittance_no_motion_rate_probe",
        "dry_run": True,
        "execute_live_contact": False,
        "execute_no_contact_velocity_probe": False,
        "motion_authorized": False,
        "velocity_command_sent": False,
        **velocity_trace_summary(rows, command_period_s=command_period_s),
    }


class Step5bVelocityAdmittanceRunner(Node):
    def __init__(self, args: argparse.Namespace, model_bundle: CalibratedModel) -> None:
        super().__init__("step5b_velocity_admittance_runner")
        self.args = args
        self.model_bundle = model_bundle
        self.latest_joint_state: JointState | None = None
        self.failure_stage = "not_started"
        self.velocity_command_sent = False
        self.trace_rows: list[dict[str, Any]] = []
        self.kunwei_monitor_snapshot: dict[str, Any] | None = None
        self.create_subscription(JointState, args.joint_state_topic, self._on_joint_state, 50)
        self.velocity_publisher = self.create_publisher(Float64MultiArray, args.velocity_command_topic, 10)

    def _on_joint_state(self, msg: JointState) -> None:
        self.latest_joint_state = msg

    def wait_for_joint_state(self) -> JointState:
        deadline = time.monotonic() + self.args.wait_s
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.05)
            if self.latest_joint_state is not None and all(name in self.latest_joint_state.name for name in JOINT_NAMES):
                return self.latest_joint_state
        raise RuntimeError(f"Timed out waiting for joint state on {self.args.joint_state_topic}")

    def run(self) -> dict[str, Any]:
        self.failure_stage = "authorization_gate"
        auth = contact_runner.run_authorization_status()
        if not bool(auth.get("authorized")):
            raise RuntimeError(f"Step5b authorization gate is not green: {auth.get('blocking_reasons')}")

        self.failure_stage = "readiness_gate"
        readiness_path = self.args.readiness_summary or contact_runner.latest_readiness_summary(self.args.runs_dir)
        if readiness_path is None:
            raise RuntimeError("No Step5b readiness summary found")
        readiness_pass, readiness_payload = contact_runner.readiness_ok(readiness_path)
        if not readiness_pass:
            raise RuntimeError(f"Step5b readiness summary is not acceptable: {readiness_path}")

        self.failure_stage = "dashboard_gate"
        dashboard = dashboard_exchange(self.args.robot_ip, ["is in remote control", "safetymode", "robotmode", "running"])
        contact_runner._validate_dashboard(dashboard)

        self.failure_stage = "controller_switch_gate"
        switch = ros2_controller_switching.ensure_forward_velocity_controller(
            self,
            controller_manager=self.args.controller_manager,
            timeout_s=self.args.wait_s,
        )
        if not switch.ok:
            raise RuntimeError(f"forward velocity controller unavailable: {switch.reason}")

        monitor = KunweiPersistentMonitor(
            KunweiMonitorConfig(
                sensor_ip=self.args.kunwei_sensor_ip,
                sensor_port=self.args.kunwei_sensor_port,
                window_s=self.args.kunwei_window_s,
                latest_max_age_s=self.args.kunwei_latest_max_age_s,
                min_recent_samples=self.args.kunwei_min_recent_samples,
                max_force_delta_n=self.args.force_norm_hard_stop_n,
                raw_frames_path=self.args.kunwei_raw_frames,
            )
        )
        params, basis = contact_runner.load_params_and_basis(self.args.stage_table, self.args.safe_frame)
        state = core.Step5bContactState()
        first_search_profile: contact_runner.FirstSearchProfile | None = None
        latch_start: float | None = None
        start = time.monotonic()
        previous = start
        monitor_finalized = False
        try:
            self.failure_stage = "kunwei_monitor_start"
            monitor.start()
            if not monitor.wait_ready(self.args.kunwei_ready_timeout_s):
                raise RuntimeError(f"Kunwei monitor did not become ready: {monitor.snapshot()['status']}")

            self.failure_stage = "live_velocity_contact_loop"
            while rclpy.ok() and time.monotonic() - start < self.args.max_runtime_s:
                now = time.monotonic()
                loop_dt_s = max(1e-6, now - previous)
                previous = now
                if loop_dt_s > self.args.rate_watchdog_max_miss_s:
                    publish_zero_velocity(self.velocity_publisher)
                    raise RuntimeError(f"rate_watchdog_miss: loop_dt_s={loop_dt_s:.6f}")

                guard = assert_monitor_ready_or_stop(monitor, self.velocity_publisher, reason_prefix="pre_publish_guard")
                if not guard.ok:
                    raise RuntimeError(guard.failure_reason)

                joint_state = self.wait_for_joint_state()
                positions = contact_runner._ordered_positions(joint_state)
                q = np.array(positions, dtype=float)
                placement = fk_tool0_base(self.model_bundle, q)
                pose = contact_runner._pose_from_placement(placement)
                snapshot = monitor.snapshot()
                tcp_wrench = contact_runner.zeroed_tcp_wrench(snapshot)
                _hard_guard_wrench(tcp_wrench, self.args.force_norm_hard_stop_n, self.args.torque_norm_hard_stop_nm)

                if not state.normal_acquired:
                    if first_search_profile is None:
                        first_search_profile = contact_runner.first_search_profile_from_pose(pose)
                    stage, search_speed_m_s, exhausted = contact_runner.first_search_stage_and_speed(pose, first_search_profile)
                    if exhausted:
                        publish_zero_velocity(self.velocity_publisher)
                        raise RuntimeError("contact_search_depth_exhausted")
                else:
                    if latch_start is None:
                        latch_start = now
                    stage = contact_runner.stage_for_elapsed(now - latch_start, state)
                    search_speed_m_s = self.args.search_speed_m_s

                command = contact_runner.compute_live_command(
                    pose=pose,
                    tcp_wrench=tcp_wrench,
                    sensor_ok=1.0 if snapshot.get("ok") else 0.0,
                    robot_stage=stage,
                    dt_s=min(self.args.velocity_command_period_s, loop_dt_s),
                    state=state,
                    params=params,
                    basis=basis,
                    search_speed_m_s=search_speed_m_s,
                )
                state = command.next_state
                if latch_start is None and state.normal_acquired:
                    latch_start = now
                qdot = [0.0] * len(JOINT_NAMES)
                published = False
                if any(abs(value) > 1e-12 for value in command.command_twist_base):
                    qdot = self._qdot_for_twist(q, command.command_twist_base)
                    publish_velocity_command(self.velocity_publisher, qdot)
                    self.velocity_command_sent = True
                    published = True
                self.trace_rows.append(
                    {
                        "t_rel_s": now - start,
                        "loop_dt_s": loop_dt_s,
                        "stage": stage,
                        "stage_slug": contact_runner.stage_slug(stage),
                        "published": published,
                        "max_abs_qdot_rad_s": max(abs(value) for value in qdot),
                        "normal_load_n": command.result.normal_load_n,
                        "force_norm_n": command.result.force_norm_n,
                        "hold_reason": command.result.hold_reason,
                    }
                )
                if state.normal_acquired and command.result.path_time_s >= params.duration_s:
                    break
                sleep_s = self.args.velocity_command_period_s - (time.monotonic() - now)
                if sleep_s > 0.0:
                    time.sleep(sleep_s)
            publish_zero_velocity(self.velocity_publisher)
            monitor.stop()
            monitor_finalized = True
            self.kunwei_monitor_snapshot = monitor.snapshot()
            _write_trace(self.args.trace, self.trace_rows)
            return self._summary(True, auth, readiness_path, readiness_payload, switch, None)
        except Exception as exc:
            publish_zero_velocity(self.velocity_publisher)
            self.kunwei_monitor_snapshot = monitor.snapshot()
            _write_trace(self.args.trace, self.trace_rows)
            raise
        finally:
            if not monitor_finalized:
                monitor.stop()

    def _qdot_for_twist(
        self,
        q: np.ndarray,
        twist_base: tuple[float, float, float, float, float, float],
    ) -> list[float]:
        pin.computeJointJacobians(self.model_bundle.model, self.model_bundle.data, q)
        pin.updateFramePlacements(self.model_bundle.model, self.model_bundle.data)
        jacobian = pin.getFrameJacobian(
            self.model_bundle.model,
            self.model_bundle.data,
            self.model_bundle.tool0_frame_id,
            pin.ReferenceFrame.LOCAL_WORLD_ALIGNED,
        )
        qdot = damped_least_squares_qdot(
            jacobian,
            twist_base,
            damping=self.args.jacobian_damping,
            max_joint_velocity_rad_s=self.args.max_joint_velocity_rad_s,
        )
        return [float(value) for value in qdot]

    def _summary(
        self,
        ok: bool,
        auth: dict[str, Any],
        readiness_path: Path,
        readiness_payload: dict[str, Any],
        switch: ros2_controller_switching.ControllerSwitchResult,
        error: str | None,
    ) -> dict[str, Any]:
        return {
            "ok": ok,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "role": "step5b_velocity_admittance_runner",
            "dry_run": False,
            "execute_live_contact": True,
            "motion_authorized": True,
            "live_runner_route": LOCKED_ROUTE,
            "contact_control_strategy": CONTACT_CONTROL_STRATEGY,
            "velocity_command_topic": self.args.velocity_command_topic,
            "velocity_command_type": "std_msgs/msg/Float64MultiArray",
            "joint_order": JOINT_NAMES,
            "velocity_command_sent": self.velocity_command_sent,
            "failure_stage": self.failure_stage,
            "error": error,
            "authorization_status": auth,
            "readiness_summary_path": str(readiness_path),
            "readiness_ok": bool(readiness_payload.get("ok")),
            "controller_switch": switch.__dict__,
            "trace_path": str(self.args.trace),
            "trace_rows": len(self.trace_rows),
            "diagnostic_summary": velocity_trace_summary(
                self.trace_rows,
                command_period_s=self.args.velocity_command_period_s,
            ),
            "kunwei_monitor": self.kunwei_monitor_snapshot,
        }


def default_run_dir() -> Path:
    return RUN_ROOT / f"step5b_velocity_admittance_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def dry_run_summary(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "ok": True,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "role": "step5b_velocity_admittance_runner",
        "dry_run": True,
        "execute_live_contact": False,
        "execute_no_contact_velocity_probe": False,
        "motion_authorized": False,
        "live_runner_route": LOCKED_ROUTE,
        "contact_control_strategy": CONTACT_CONTROL_STRATEGY,
        "velocity_command_topic": args.velocity_command_topic,
        "velocity_command_type": "std_msgs/msg/Float64MultiArray",
        "joint_order": JOINT_NAMES,
        "target_command_period_s": args.velocity_command_period_s,
        "target_command_rate_hz": 1.0 / args.velocity_command_period_s,
        "velocity_command_sent": False,
        "acceptance_contract": acceptance_contract(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Step5b ROS2 Remote Control/headless velocity-admittance contact runner.")
    parser.add_argument("--describe-acceptance", action="store_true")
    parser.add_argument("--execute-live-contact", action="store_true")
    parser.add_argument("--execute-no-contact-velocity-probe", action="store_true")
    parser.add_argument("--no-motion-rate-probe", action="store_true")
    parser.add_argument("--no-motion-probe-duration-s", type=float, default=2.0)
    parser.add_argument("--robot-ip", default="192.168.1.18")
    parser.add_argument("--controller-manager", default="/controller_manager")
    parser.add_argument("--velocity-command-topic", default=DEFAULT_COMMAND_TOPIC)
    parser.add_argument("--joint-state-topic", default="/joint_states")
    parser.add_argument("--wait-s", type=float, default=15.0)
    parser.add_argument("--max-runtime-s", type=float, default=120.0)
    parser.add_argument("--velocity-command-period-s", type=float, default=0.002)
    parser.add_argument("--max-joint-velocity-rad-s", type=float, default=0.05)
    parser.add_argument("--jacobian-damping", type=float, default=1e-3)
    parser.add_argument("--rate-watchdog-max-miss-s", type=float, default=0.020)
    parser.add_argument("--search-speed-m-s", type=float, default=0.001)
    parser.add_argument("--force-norm-hard-stop-n", type=float, default=60.0)
    parser.add_argument("--torque-norm-hard-stop-nm", type=float, default=3.0)
    parser.add_argument("--kunwei-sensor-ip", default="192.168.50.25")
    parser.add_argument("--kunwei-sensor-port", type=int, default=5152)
    parser.add_argument("--kunwei-ready-timeout-s", type=float, default=3.0)
    parser.add_argument("--kunwei-window-s", type=float, default=0.5)
    parser.add_argument("--kunwei-latest-max-age-s", type=float, default=0.25)
    parser.add_argument("--kunwei-min-recent-samples", type=int, default=20)
    parser.add_argument("--calibration-yaml", type=Path, default=DEFAULT_CALIBRATION_YAML)
    parser.add_argument("--xacro-path", type=Path, default=DEFAULT_XACRO_PATH)
    parser.add_argument("--stage-table", type=Path, default=contact_runner.STAGE_TABLE)
    parser.add_argument("--safe-frame", type=Path, default=contact_runner.SAFE_FRAME)
    parser.add_argument("--runs-dir", type=Path, default=RUN_ROOT)
    parser.add_argument("--readiness-summary", type=Path, default=None)
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument("--summary", type=Path, default=None)
    parser.add_argument("--trace", type=Path, default=None)
    parser.add_argument("--kunwei-raw-frames", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.describe_acceptance:
        print(json.dumps(acceptance_contract(), indent=2, sort_keys=True))
        return 0

    run_dir = args.run_dir or default_run_dir()
    args.summary = args.summary or run_dir / "summary.json"
    args.trace = args.trace or run_dir / "step5b_velocity_admittance_trace.csv"
    args.kunwei_raw_frames = args.kunwei_raw_frames or run_dir / "kunwei_raw_frames.bin"

    if args.execute_no_contact_velocity_probe and not args.execute_live_contact:
        payload = {
            "ok": False,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "role": "step5b_velocity_admittance_runner",
            "execute_no_contact_velocity_probe": True,
            "velocity_command_sent": False,
            "error": "execute_no_contact_velocity_probe_requires_auditor_authorized_execute_live_contact",
        }
        _write_summary(args.summary, payload)
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        return 2

    if args.no_motion_rate_probe:
        payload = measure_no_motion_rate(
            command_period_s=args.velocity_command_period_s,
            duration_s=args.no_motion_probe_duration_s,
        )
        _write_summary(args.summary, payload)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    if not args.execute_live_contact:
        payload = dry_run_summary(args)
        _write_summary(args.summary, payload)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    model_bundle = build_calibrated_model(args.calibration_yaml, args.xacro_path)
    if model_bundle.calibration_hash != EXPECTED_CALIBRATION_HASH:
        raise SystemExit(f"unexpected calibration hash: {model_bundle.calibration_hash}")
    rclpy.init(args=None)
    node = Step5bVelocityAdmittanceRunner(args, model_bundle)
    try:
        payload = node.run()
        _write_summary(args.summary, payload)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        payload = {
            "ok": False,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "role": "step5b_velocity_admittance_runner",
            "dry_run": False,
            "execute_live_contact": True,
            "motion_authorized": False,
            "live_runner_route": LOCKED_ROUTE,
            "contact_control_strategy": CONTACT_CONTROL_STRATEGY,
            "velocity_command_topic": args.velocity_command_topic,
            "velocity_command_type": "std_msgs/msg/Float64MultiArray",
            "velocity_command_sent": bool(getattr(node, "velocity_command_sent", False)),
            "failure_stage": getattr(node, "failure_stage", "unknown"),
            "error": f"{type(exc).__name__}: {exc}",
            "trace_path": str(args.trace),
            "trace_rows": len(getattr(node, "trace_rows", [])),
            "diagnostic_summary": velocity_trace_summary(
                getattr(node, "trace_rows", []),
                command_period_s=args.velocity_command_period_s,
            ),
            "kunwei_monitor": getattr(node, "kunwei_monitor_snapshot", None),
        }
        _write_summary(args.summary, payload)
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        return 2
    finally:
        node.destroy_node()
        rclpy.shutdown()


def _hard_guard_wrench(wrench: tuple[float, float, float, float, float, float], force_limit_n: float, torque_limit_nm: float) -> None:
    force_norm = math.sqrt(sum(value * value for value in wrench[:3]))
    torque_norm = math.sqrt(sum(value * value for value in wrench[3:]))
    if force_norm >= force_limit_n:
        raise RuntimeError(f"force_norm_hard_stop: {force_norm:.3f} N >= {force_limit_n:.3f} N")
    if torque_norm >= torque_limit_nm:
        raise RuntimeError(f"torque_norm_hard_stop: {torque_norm:.3f} Nm >= {torque_limit_nm:.3f} Nm")


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    return float(np.percentile(np.array(values, dtype=float), percentile))


def _write_summary(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_trace(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "t_rel_s",
        "loop_dt_s",
        "stage",
        "stage_slug",
        "published",
        "max_abs_qdot_rad_s",
        "normal_load_n",
        "force_norm_n",
        "hold_reason",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


if __name__ == "__main__":
    raise SystemExit(main())
