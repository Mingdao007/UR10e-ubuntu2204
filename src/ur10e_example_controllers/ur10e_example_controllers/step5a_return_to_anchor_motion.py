from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import rclpy
from control_msgs.action import FollowJointTrajectory
from rclpy.action import ActionClient
from rclpy.node import Node
from sensor_msgs.msg import JointState
from trajectory_msgs.msg import JointTrajectoryPoint

from .kunwei_persistent_monitor import KunweiMonitorConfig, KunweiPersistentMonitor
from .step5a_cartesian_cycloid_motion import (
    COMMAND_FIELDS,
    COMMAND_FK_FIELDS,
    DEFAULT_CALIBRATION_YAML,
    DEFAULT_XACRO_PATH,
    JOINT_NAMES,
    build_calibrated_model,
    dashboard_exchange,
    fk_tool0_base,
)


DEFAULT_SOURCE_SUMMARY = "step5a_cartesian_cycloid_motion.json"


class Step5aReturnToAnchorMotion(Node):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__("step5a_return_to_anchor_motion")
        self.args = args
        self.model_bundle = build_calibrated_model(args.calibration_yaml, args.xacro_path)
        self.joint_state: JointState | None = None
        self.sent_goal = False
        self.accepted = False
        self.result_status: int | None = None
        self.result_error_code: int | None = None
        self.result_error_string: str | None = None
        self.failure_stage = "not_started"
        self.trajectory_authority_entered = False
        self.create_subscription(JointState, args.joint_state_topic, self._on_joint_state, 20)
        self.action_client = ActionClient(self, FollowJointTrajectory, args.action_name)

    def _on_joint_state(self, msg: JointState) -> None:
        self.joint_state = msg

    def run(self) -> dict[str, Any]:
        source = load_source_summary(self.args.source_run_dir)
        target_positions = source_start_positions(source)
        dashboard: dict[str, str] = {}
        monitor = KunweiPersistentMonitor(
            KunweiMonitorConfig(
                sensor_ip=self.args.kunwei_sensor_ip,
                sensor_port=self.args.kunwei_sensor_port,
                connect_timeout_s=self.args.kunwei_ready_timeout_s,
                window_s=self.args.kunwei_window_s,
                latest_max_age_s=self.args.kunwei_latest_max_age_s,
                min_recent_samples=self.args.kunwei_min_recent_samples,
                max_force_delta_n=self.args.kunwei_max_force_delta_n,
                raw_frames_path=self.args.kunwei_raw_frames,
            )
        )
        trace_rows: list[dict[str, Any]] = []
        start_positions: list[float] = []
        monitor_finalized = False
        try:
            self.failure_stage = "kunwei_monitor_start"
            monitor.start()
            if not monitor.wait_ready(self.args.kunwei_ready_timeout_s):
                raise RuntimeError("Kunwei persistent monitor did not become ready")

            self.failure_stage = "dashboard"
            dashboard = dashboard_exchange(
                self.args.robot_ip,
                ["is in remote control", "safetymode", "robotmode", "running"],
            )
            if dashboard.get("is in remote control") != "true":
                raise RuntimeError(f"Remote Control is not true: {dashboard.get('is in remote control')}")
            if "NORMAL" not in dashboard.get("safetymode", ""):
                raise RuntimeError(f"Safety mode is not NORMAL: {dashboard.get('safetymode')}")

            self.failure_stage = "joint_state"
            current = self.wait_for_joint_state()
            start_positions = ordered_positions(current)
            points, trace_rows, metrics = build_return_trajectory(
                self.model_bundle,
                start_positions,
                target_positions,
                dt=self.args.sample_period_s,
                min_duration_s=self.args.min_duration_s,
                max_joint_speed_rad_s=self.args.max_joint_speed_rad_s,
                max_cartesian_speed_m_s=self.args.max_cartesian_speed_m_s,
            )
            write_trace(self.args.trace, trace_rows)

            if not self.args.execute:
                monitor.stop()
                monitor_finalized = True
                monitor.write_summary(self.args.kunwei_summary, self._kunwei_extra(False))
                return self._summary(source, dashboard, monitor.snapshot(), start_positions, target_positions, trace_rows, metrics)

            self.failure_stage = "pre_send_kunwei_gate"
            monitor.assert_fresh_and_within_force_delta()
            self.failure_stage = "action_server"
            if not self.action_client.wait_for_server(timeout_sec=self.args.wait_s):
                raise RuntimeError(f"Action server unavailable: {self.args.action_name}")

            goal = FollowJointTrajectory.Goal()
            goal.trajectory.joint_names = list(JOINT_NAMES)
            goal.trajectory.points = points
            self.failure_stage = "send_goal"
            self.sent_goal = True
            send_future = self.action_client.send_goal_async(goal)
            rclpy.spin_until_future_complete(self, send_future, timeout_sec=self.args.wait_s)
            if not send_future.done() or send_future.result() is None:
                raise RuntimeError("Timed out sending return-to-anchor goal")
            goal_handle = send_future.result()
            self.accepted = bool(goal_handle.accepted)
            if not goal_handle.accepted:
                raise RuntimeError("Return-to-anchor goal was rejected")

            self.trajectory_authority_entered = True
            self.failure_stage = "trajectory_execution"
            result_future = goal_handle.get_result_async()
            deadline = time.monotonic() + float(metrics["duration_s"]) + self.args.wait_s
            while rclpy.ok() and not result_future.done() and time.monotonic() < deadline:
                rclpy.spin_once(self, timeout_sec=0.05)
                monitor.assert_fresh_and_within_force_delta()
            if not result_future.done():
                raise RuntimeError("Timed out waiting for return-to-anchor result")
            result = result_future.result()
            self.result_status = int(result.status)
            self.result_error_code = int(result.result.error_code)
            self.result_error_string = str(result.result.error_string)
            for _ in range(50):
                rclpy.spin_once(self, timeout_sec=0.02)
            monitor.stop()
            monitor_finalized = True
            monitor.write_summary(self.args.kunwei_summary, self._kunwei_extra(True))
            metrics["final_joint_error_max_rad"] = max(
                abs(a - b) for a, b in zip(ordered_positions(self.joint_state), target_positions)
            )
            return self._summary(
                source,
                dashboard,
                json.loads(self.args.kunwei_summary.read_text(encoding="utf-8")),
                start_positions,
                target_positions,
                trace_rows,
                metrics,
            )
        finally:
            if not monitor_finalized:
                monitor.stop()
                monitor.write_summary(self.args.kunwei_summary, self._kunwei_extra(bool(self.args.execute)))

    def wait_for_joint_state(self) -> JointState:
        deadline = time.monotonic() + self.args.wait_s
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.1)
            if self.joint_state is not None and all(name in self.joint_state.name for name in JOINT_NAMES):
                return self.joint_state
        raise RuntimeError("Timed out waiting for UR10e joint_states")

    def _kunwei_extra(self, live_motion: bool) -> dict[str, Any]:
        return {
            "live_motion": live_motion,
            "failure_stage": self.failure_stage,
            "sent_goal": self.sent_goal,
            "accepted": self.accepted,
            "result_status": self.result_status,
            "result_error_code": self.result_error_code,
            "result_error_string": self.result_error_string,
            "trajectory_authority_entered": self.trajectory_authority_entered,
        }

    def _summary(
        self,
        source: dict[str, Any],
        dashboard: dict[str, str],
        kunwei_snapshot: dict[str, Any],
        start_positions: list[float],
        target_positions: list[float],
        trace_rows: list[dict[str, Any]],
        metrics: dict[str, Any],
    ) -> dict[str, Any]:
        final_joint_error = metrics.get("final_joint_error_max_rad")
        ok = (
            (not self.args.execute)
            or (
                self.sent_goal
                and self.accepted
                and self.result_error_code == FollowJointTrajectory.Result.SUCCESSFUL
                and final_joint_error is not None
                and float(final_joint_error) <= self.args.goal_joint_tolerance_rad
            )
        )
        return {
            "ok": bool(ok),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "role": "step5a_live_return_to_anchor_not_gate_a_acceptance",
            "motion_kind": "joint_return_to_recorded_start_positions",
            "source_run_dir": str(self.args.source_run_dir),
            "source_summary_path": str((self.args.source_run_dir / DEFAULT_SOURCE_SUMMARY).resolve()),
            "source_stage_id": source.get("stage_id"),
            "source_role": source.get("role"),
            "source_motion_kind": source.get("motion_kind"),
            "execute": bool(self.args.execute),
            "live_air_motion_authorized": bool(self.args.execute),
            "step5a_acceptance": "not_applicable_return_to_anchor_only",
            "contact_policy": source.get("contact_policy", {}),
            "force_source": "kunwei_force_monitor",
            "kunwei_monitor": kunwei_snapshot,
            "kunwei_artifact_ok": bool(
                kunwei_snapshot.get("ok") is True
                and kunwei_snapshot.get("stream_start_command_sent") is True
                and kunwei_snapshot.get("stream_stop_command_sent") is True
            ),
            "robot_ip": self.args.robot_ip,
            "action_name": self.args.action_name,
            "dashboard": dashboard,
            "rows": len(trace_rows),
            "duration_s": metrics["duration_s"],
            "sample_period_s": self.args.sample_period_s,
            "max_joint_delta_rad": metrics["max_joint_delta_rad"],
            "max_joint_speed_command_rad_s": metrics["max_joint_speed_command_rad_s"],
            "max_commanded_fk_speed_m_s": metrics["max_commanded_fk_speed_m_s"],
            "max_cartesian_speed_m_s": self.args.max_cartesian_speed_m_s,
            "final_joint_error_max_rad": final_joint_error,
            "goal_joint_tolerance_rad": self.args.goal_joint_tolerance_rad,
            "start_positions": dict(zip(JOINT_NAMES, start_positions)),
            "target_positions": dict(zip(JOINT_NAMES, target_positions)),
            "sent_goal": self.sent_goal,
            "accepted": self.accepted,
            "result_status": self.result_status,
            "result_error_code": self.result_error_code,
            "result_error_string": self.result_error_string,
            "failure_stage": self.failure_stage,
            "trajectory_authority_entered": self.trajectory_authority_entered,
            "trace_path": str(self.args.trace),
            "trace_fields": RETURN_TRACE_FIELDS,
        }


RETURN_TRACE_FIELDS = [
    "row_index",
    "t_rel_s",
    *COMMAND_FIELDS,
    *COMMAND_FK_FIELDS,
]


def build_return_trajectory(
    model_bundle: Any,
    start_positions: list[float],
    target_positions: list[float],
    *,
    dt: float,
    min_duration_s: float,
    max_joint_speed_rad_s: float,
    max_cartesian_speed_m_s: float,
) -> tuple[list[JointTrajectoryPoint], list[dict[str, Any]], dict[str, Any]]:
    start = np.array(start_positions, dtype=float)
    target = np.array(target_positions, dtype=float)
    delta = target - start
    max_joint_delta = float(np.max(np.abs(delta))) if len(delta) else 0.0
    duration = max(min_duration_s, max_joint_delta / max_joint_speed_rad_s if max_joint_speed_rad_s > 0 else min_duration_s)

    for _ in range(4):
        points, trace_rows, max_fk_speed = _sample_return(model_bundle, start, target, duration, dt)
        if max_fk_speed <= max_cartesian_speed_m_s + 1e-12 or max_fk_speed <= 0.0:
            break
        duration *= 1.15 * max_fk_speed / max_cartesian_speed_m_s
    points, trace_rows, max_fk_speed = _sample_return(model_bundle, start, target, duration, dt)
    return points, trace_rows, {
        "duration_s": duration,
        "max_joint_delta_rad": max_joint_delta,
        "max_joint_speed_command_rad_s": max_joint_delta / duration if duration > 0 else 0.0,
        "max_commanded_fk_speed_m_s": max_fk_speed,
        "final_joint_error_max_rad": None,
    }


def _sample_return(
    model_bundle: Any,
    start: np.ndarray,
    target: np.ndarray,
    duration: float,
    dt: float,
) -> tuple[list[JointTrajectoryPoint], list[dict[str, Any]], float]:
    count = max(2, int(math.ceil(duration / dt)) + 1)
    points: list[JointTrajectoryPoint] = []
    trace_rows: list[dict[str, Any]] = []
    fk_xyz: list[np.ndarray] = []
    sample_times: list[float] = []
    delta = target - start
    qdot = delta / duration if duration > 0 else np.zeros_like(delta)
    for index in range(count):
        t_rel_s = min(duration, index * dt)
        sample_times.append(t_rel_s)
        blend = 1.0 if duration <= 0 else min(1.0, t_rel_s / duration)
        q = start + blend * delta
        placement = fk_tool0_base(model_bundle, q)
        fk_xyz.append(placement.translation.copy())
        point = JointTrajectoryPoint()
        point.positions = [float(value) for value in q]
        point.velocities = [0.0 for _ in JOINT_NAMES] if index in {0, count - 1} else [float(value) for value in qdot]
        point.time_from_start.sec = int(t_rel_s)
        point.time_from_start.nanosec = int(round((t_rel_s - int(t_rel_s)) * 1_000_000_000))
        if point.time_from_start.nanosec >= 1_000_000_000:
            point.time_from_start.sec += 1
            point.time_from_start.nanosec -= 1_000_000_000
        points.append(point)
        row = {"row_index": index, "t_rel_s": t_rel_s}
        for name, value in zip(COMMAND_FIELDS, q):
            row[name] = float(value)
        row["commanded_fk_x_m"] = float(placement.translation[0])
        row["commanded_fk_y_m"] = float(placement.translation[1])
        row["commanded_fk_z_m"] = float(placement.translation[2])
        trace_rows.append(row)
    speeds = [
        float(np.linalg.norm(fk_xyz[index] - fk_xyz[index - 1]) / (sample_times[index] - sample_times[index - 1]))
        for index in range(1, len(fk_xyz))
        if sample_times[index] > sample_times[index - 1]
    ]
    return points, trace_rows, max(speeds) if speeds else 0.0


def load_source_summary(source_run_dir: Path) -> dict[str, Any]:
    source_run_dir = source_run_dir.expanduser().resolve()
    path = source_run_dir / DEFAULT_SOURCE_SUMMARY
    if not path.exists():
        raise FileNotFoundError(f"source Step5a summary is missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def source_start_positions(source: dict[str, Any]) -> list[float]:
    payload = source.get("start_positions")
    if not isinstance(payload, dict):
        raise RuntimeError("source summary has no start_positions dictionary")
    missing = [name for name in JOINT_NAMES if name not in payload]
    if missing:
        raise RuntimeError(f"source start_positions missing joints: {missing}")
    return [float(payload[name]) for name in JOINT_NAMES]


def ordered_positions(joint_state: JointState | None) -> list[float]:
    if joint_state is None:
        raise RuntimeError("no joint state available")
    by_name = dict(zip(joint_state.name, joint_state.position))
    return [float(by_name[name]) for name in JOINT_NAMES]


def write_trace(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RETURN_TRACE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Return UR10e to the recorded Step5a anchor joint positions.")
    parser.add_argument("--source-run-dir", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--robot-ip", default="192.168.1.18")
    parser.add_argument("--action-name", default="/scaled_joint_trajectory_controller/follow_joint_trajectory")
    parser.add_argument("--joint-state-topic", default="/joint_states")
    parser.add_argument("--wait-s", type=float, default=30.0)
    parser.add_argument("--sample-period-s", type=float, default=0.02)
    parser.add_argument("--min-duration-s", type=float, default=12.0)
    parser.add_argument("--max-joint-speed-rad-s", type=float, default=0.05)
    parser.add_argument("--max-cartesian-speed-m-s", type=float, default=0.004)
    parser.add_argument("--goal-joint-tolerance-rad", type=float, default=0.01)
    parser.add_argument("--calibration-yaml", type=Path, default=DEFAULT_CALIBRATION_YAML)
    parser.add_argument("--xacro-path", type=Path, default=DEFAULT_XACRO_PATH)
    parser.add_argument("--kunwei-sensor-ip", default="192.168.50.25")
    parser.add_argument("--kunwei-sensor-port", type=int, default=5152)
    parser.add_argument("--kunwei-ready-timeout-s", type=float, default=3.0)
    parser.add_argument("--kunwei-window-s", type=float, default=0.5)
    parser.add_argument("--kunwei-latest-max-age-s", type=float, default=0.25)
    parser.add_argument("--kunwei-min-recent-samples", type=int, default=20)
    parser.add_argument("--kunwei-max-force-delta-n", type=float, default=8.0)
    parser.add_argument("--kunwei-summary", type=Path, required=True)
    parser.add_argument("--kunwei-raw-frames", type=Path, default=None)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args(argv)

    rclpy.init(args=None)
    node = Step5aReturnToAnchorMotion(args)
    try:
        summary = node.run()
        text = json.dumps(summary, indent=2, sort_keys=True) + "\n"
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(text, encoding="utf-8")
        print(text, end="")
        return 0 if summary["ok"] else 2
    except Exception as exc:
        payload = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "role": "step5a_live_return_to_anchor_not_gate_a_acceptance",
            "motion_kind": "joint_return_to_recorded_start_positions",
            "source_run_dir": str(args.source_run_dir),
            "sent_goal": bool(getattr(node, "sent_goal", False)),
            "accepted": bool(getattr(node, "accepted", False)),
            "result_status": getattr(node, "result_status", None),
            "result_error_code": getattr(node, "result_error_code", None),
            "result_error_string": getattr(node, "result_error_string", None),
            "failure_stage": getattr(node, "failure_stage", "unknown"),
            "trajectory_authority_entered": bool(getattr(node, "trajectory_authority_entered", False)),
            "force_source": "kunwei_force_monitor",
        }
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(text, encoding="utf-8")
        print(text, end="", file=sys.stderr)
        return 2
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
