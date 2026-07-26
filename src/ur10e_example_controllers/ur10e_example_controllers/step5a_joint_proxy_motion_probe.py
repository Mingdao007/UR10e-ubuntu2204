from __future__ import annotations

import argparse
import csv
import json
import math
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import rclpy
from control_msgs.action import FollowJointTrajectory
from rclpy.action import ActionClient
from rclpy.node import Node
from sensor_msgs.msg import JointState
from trajectory_msgs.msg import JointTrajectoryPoint

from .kunwei_persistent_monitor import KunweiMonitorConfig, KunweiPersistentMonitor
from .no_contact_cycloid_shadow import DEFAULT_CONFIG, TRACE_FIELDS, _iter_reference_rows, load_no_contact_config


JOINT_NAMES = [
    "shoulder_pan_joint",
    "shoulder_lift_joint",
    "elbow_joint",
    "wrist_1_joint",
    "wrist_2_joint",
    "wrist_3_joint",
]

LIVE_TRACE_FIELDS = TRACE_FIELDS + [
    "command_joint_name",
    "command_joint_position_rad",
    "command_joint_velocity_rad_s",
    "kunwei_force_delta_n",
]


def dashboard_exchange(host: str, commands: list[str], *, port: int = 29999, timeout_s: float = 2.0) -> dict[str, str]:
    responses: dict[str, str] = {}
    with socket.create_connection((host, port), timeout=timeout_s) as sock:
        sock.settimeout(timeout_s)
        responses["banner"] = sock.recv(4096).decode("utf-8", errors="replace").strip()
        for command in commands:
            sock.sendall((command + "\n").encode("utf-8"))
            responses[command] = sock.recv(4096).decode("utf-8", errors="replace").strip()
    return responses


class Step5aJointProxyMotionProbe(Node):
    def __init__(self, args: argparse.Namespace, config: dict[str, Any]) -> None:
        super().__init__("step5a_joint_proxy_motion_probe")
        self.args = args
        self.config = config
        self.joint_state: JointState | None = None
        self.create_subscription(JointState, args.joint_state_topic, self._on_joint_state, 10)
        self.action_client = ActionClient(self, FollowJointTrajectory, args.action_name)

    def _on_joint_state(self, msg: JointState) -> None:
        self.joint_state = msg

    def wait_for_joint_state(self) -> JointState:
        deadline = time.monotonic() + self.args.wait_s
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.1)
            if self.joint_state is not None and all(name in self.joint_state.name for name in JOINT_NAMES):
                return self.joint_state
        raise RuntimeError(f"Timed out waiting for joint state on {self.args.joint_state_topic}")

    def run(self) -> dict[str, Any]:
        dashboard = dashboard_exchange(self.args.robot_ip, ["is in remote control", "safetymode", "robotmode", "running"])
        _validate_dashboard(dashboard)
        joint_state = self.wait_for_joint_state()
        start_positions = _ordered_positions(joint_state)
        if not self.action_client.wait_for_server(timeout_sec=self.args.wait_s):
            raise RuntimeError(f"Action server unavailable: {self.args.action_name}")

        monitor = KunweiPersistentMonitor(
            KunweiMonitorConfig(
                sensor_ip=self.args.kunwei_sensor_ip,
                sensor_port=self.args.kunwei_sensor_port,
                window_s=self.args.kunwei_window_s,
                latest_max_age_s=self.args.kunwei_latest_max_age_s,
                min_recent_samples=self.args.kunwei_min_recent_samples,
                max_force_delta_n=self.args.kunwei_max_force_delta_n,
                raw_frames_path=self.args.kunwei_raw_frames,
            )
        )
        sent_goal = False
        accepted = False
        result_status = None
        result_error_code = None
        result_error_string = None
        canceled_by_kunwei = False
        trace_rows: list[dict[str, Any]] = []
        try:
            monitor.start()
            if not monitor.wait_ready(self.args.kunwei_ready_timeout_s):
                raise RuntimeError(f"Kunwei persistent monitor did not become ready: {monitor.snapshot()['status']}")

            goal = FollowJointTrajectory.Goal()
            goal.trajectory.joint_names = JOINT_NAMES
            trajectory_points, trace_rows = _joint_proxy_trajectory(
                self.config,
                start_positions,
                self.args.joint_name,
                self.args.joint_delta_rad,
            )
            goal.trajectory.points = trajectory_points
            _write_trace(self.args.trace, trace_rows)

            if not self.args.execute:
                return self._summary(
                    dashboard=dashboard,
                    monitor=monitor,
                    start_positions=start_positions,
                    trace_rows=trace_rows,
                    sent_goal=False,
                    accepted=False,
                    result_status=None,
                    result_error_code=None,
                    result_error_string=None,
                    canceled_by_kunwei=False,
                )

            monitor.assert_fresh_and_within_force_delta()
            send_future = self.action_client.send_goal_async(goal)
            rclpy.spin_until_future_complete(self, send_future, timeout_sec=self.args.wait_s)
            goal_handle = send_future.result()
            if goal_handle is None:
                raise RuntimeError("FollowJointTrajectory goal did not return a handle")
            sent_goal = True
            accepted = bool(goal_handle.accepted)
            if not goal_handle.accepted:
                raise RuntimeError("FollowJointTrajectory goal was rejected")

            result_future = goal_handle.get_result_async()
            deadline = time.monotonic() + float(self.config["duration_s"]) + self.args.wait_s
            while rclpy.ok() and time.monotonic() < deadline and not result_future.done():
                rclpy.spin_once(self, timeout_sec=0.05)
                try:
                    monitor.assert_fresh_and_within_force_delta()
                except RuntimeError:
                    canceled_by_kunwei = True
                    cancel_future = goal_handle.cancel_goal_async()
                    rclpy.spin_until_future_complete(self, cancel_future, timeout_sec=1.0)
                    raise

            result = result_future.result()
            if result is None:
                raise RuntimeError("Timed out waiting for FollowJointTrajectory result")
            result_status = int(result.status)
            result_error_code = int(result.result.error_code)
            result_error_string = str(result.result.error_string)
            if result.result.error_code != FollowJointTrajectory.Result.SUCCESSFUL:
                raise RuntimeError(f"Trajectory failed: {result.result.error_code} {result.result.error_string}")
            return self._summary(
                dashboard=dashboard,
                monitor=monitor,
                start_positions=start_positions,
                trace_rows=trace_rows,
                sent_goal=sent_goal,
                accepted=accepted,
                result_status=result_status,
                result_error_code=result_error_code,
                result_error_string=result_error_string,
                canceled_by_kunwei=canceled_by_kunwei,
            )
        finally:
            monitor.stop()
            monitor.write_summary(
                self.args.kunwei_summary,
                {
                    "live_motion": bool(self.args.execute),
                    "sent_goal": sent_goal,
                    "accepted": accepted,
                    "result_status": result_status,
                    "result_error_code": result_error_code,
                    "result_error_string": result_error_string,
                    "canceled_by_kunwei": canceled_by_kunwei,
                },
            )

    def _summary(
        self,
        *,
        dashboard: dict[str, str],
        monitor: KunweiPersistentMonitor,
        start_positions: list[float],
        trace_rows: list[dict[str, Any]],
        sent_goal: bool,
        accepted: bool,
        result_status: int | None,
        result_error_code: int | None,
        result_error_string: str | None,
        canceled_by_kunwei: bool,
    ) -> dict[str, Any]:
        speeds = [float(row["reference_speed_m_s"]) for row in trace_rows]
        return {
            "ok": bool((not self.args.execute) or (sent_goal and accepted and result_error_code == 0)),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "stage_id": self.config.get("stage_id", "step5a_ros2_remote_no_contact_v1"),
            "role": "step5a_live_no_contact_air_motion_joint_proxy",
            "motion_kind": "joint_proxy_cycloid_timing_not_cartesian_cycloid",
            "step5a_status": "live_no_contact_motion_exercised_but_cartesian_cycloid_equivalence_still_open",
            "execute": bool(self.args.execute),
            "live_air_motion_authorized": bool(self.args.execute),
            "contact_policy": dict(self.config.get("contact_policy", {})),
            "force_source": "kunwei_force_monitor",
            "kunwei_monitor": monitor.snapshot(),
            "ur_internal_force_delta_advisory": {"enabled": False, "status": "not_required_for_step5a_force_source"},
            "robot_ip": self.args.robot_ip,
            "action_name": self.args.action_name,
            "dashboard": dashboard,
            "joint_proxy": {
                "joint_name": self.args.joint_name,
                "joint_delta_rad": self.args.joint_delta_rad,
                "hard_delta_cap_rad": self.args.joint_delta_cap_rad,
            },
            "start_positions": dict(zip(JOINT_NAMES, start_positions)),
            "sent_goal": sent_goal,
            "accepted": accepted,
            "result_status": result_status,
            "result_error_code": result_error_code,
            "result_error_string": result_error_string,
            "canceled_by_kunwei": canceled_by_kunwei,
            "trace_path": str(self.args.trace),
            "trace_rows": len(trace_rows),
            "trace_fields": LIVE_TRACE_FIELDS,
            "step5a_reference_parameters": _reference_parameters(self.config),
            "max_reference_speed_m_s": max(speeds) if speeds else math.nan,
            "velocity_cap_m_s": float(self.config["velocity_cap_m_s"]),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an explicit Step5a live no-contact joint-proxy motion.")
    parser.add_argument("--execute", action="store_true", help="Actually send the trajectory goal.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--robot-ip", default="192.168.1.18")
    parser.add_argument("--action-name", default="/scaled_joint_trajectory_controller/follow_joint_trajectory")
    parser.add_argument("--joint-state-topic", default="/joint_states")
    parser.add_argument("--joint-name", default="wrist_3_joint", choices=JOINT_NAMES)
    parser.add_argument("--joint-delta-rad", type=float, default=0.04)
    parser.add_argument("--joint-delta-cap-rad", type=float, default=0.08)
    parser.add_argument("--wait-s", type=float, default=15.0)
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

    if abs(args.joint_delta_rad) > args.joint_delta_cap_rad:
        raise SystemExit(f"joint_delta_rad exceeds cap: {args.joint_delta_rad} > {args.joint_delta_cap_rad}")

    config = load_no_contact_config(args.config)
    rclpy.init(args=None)
    node = Step5aJointProxyMotionProbe(args, config)
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
            "stage_id": config.get("stage_id", "step5a_ros2_remote_no_contact_v1"),
            "role": "step5a_live_no_contact_air_motion_joint_proxy",
            "sent_goal": False,
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


def _joint_proxy_trajectory(
    config: dict[str, Any],
    start_positions: list[float],
    joint_name: str,
    joint_delta_rad: float,
) -> tuple[list[JointTrajectoryPoint], list[dict[str, Any]]]:
    rows = _iter_reference_rows(config)
    final_x = max(abs(float(row["desired_x_m"])) for row in rows) or 1.0
    joint_index = JOINT_NAMES.index(joint_name)
    points: list[JointTrajectoryPoint] = []
    trace_rows: list[dict[str, Any]] = []
    for row in rows:
        scale = float(row["desired_x_m"]) / final_x
        positions = list(start_positions)
        positions[joint_index] += joint_delta_rad * scale
        velocity_scale = float(row["desired_vx_m_s"]) / final_x
        velocities = [0.0] * len(JOINT_NAMES)
        velocities[joint_index] = joint_delta_rad * velocity_scale
        point = JointTrajectoryPoint()
        point.positions = positions
        point.velocities = velocities
        t_rel_s = float(row["t_rel_s"])
        point.time_from_start.sec = int(t_rel_s)
        point.time_from_start.nanosec = int((t_rel_s - int(t_rel_s)) * 1_000_000_000)
        points.append(point)
        trace_row = dict(row)
        trace_row.update(
            {
                "cmd_enabled": True,
                "command_joint_name": joint_name,
                "command_joint_position_rad": positions[joint_index],
                "command_joint_velocity_rad_s": velocities[joint_index],
                "kunwei_force_delta_n": "",
            }
        )
        trace_rows.append(trace_row)
    return points, trace_rows


def _write_trace(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=LIVE_TRACE_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in LIVE_TRACE_FIELDS})


def _ordered_positions(joint_state: JointState) -> list[float]:
    by_name = {name: joint_state.position[index] for index, name in enumerate(joint_state.name)}
    return [float(by_name[name]) for name in JOINT_NAMES]


def _validate_dashboard(responses: dict[str, str]) -> None:
    if responses.get("is in remote control") != "true":
        raise RuntimeError(f"Remote Control is not true: {responses.get('is in remote control')}")
    if "NORMAL" not in responses.get("safetymode", ""):
        raise RuntimeError(f"Safety mode is not NORMAL: {responses.get('safetymode')}")
    if "RUNNING" not in responses.get("robotmode", ""):
        raise RuntimeError(f"Robot mode is not RUNNING: {responses.get('robotmode')}")
    running = responses.get("running")
    if running not in {"Program running: false", "Program running: true"}:
        raise RuntimeError(f"Unexpected Dashboard running state: {running}")


def _reference_parameters(config: dict[str, Any]) -> dict[str, float]:
    keys = [
        "duration_s",
        "sample_period_s",
        "amplitude_m",
        "omega_rad_s",
        "final_phase_rad",
        "fixed_base_z_m",
        "velocity_cap_m_s",
        "warmup_hold_s",
        "fast_hold_s",
        "fast_after_s",
    ]
    return {key: float(config[key]) for key in keys}


if __name__ == "__main__":
    raise SystemExit(main())
