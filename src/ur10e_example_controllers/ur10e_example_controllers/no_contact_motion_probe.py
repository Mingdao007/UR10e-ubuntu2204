from __future__ import annotations

import argparse
import json
import math
import socket
import sys
import time
from pathlib import Path
from typing import Any

import rclpy
from control_msgs.action import FollowJointTrajectory
from geometry_msgs.msg import WrenchStamped
from rclpy.action import ActionClient
from rclpy.node import Node
from sensor_msgs.msg import JointState
from trajectory_msgs.msg import JointTrajectoryPoint


JOINT_NAMES = [
    "shoulder_pan_joint",
    "shoulder_lift_joint",
    "elbow_joint",
    "wrist_1_joint",
    "wrist_2_joint",
    "wrist_3_joint",
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


class NoContactMotionProbe(Node):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__("no_contact_motion_probe")
        self.args = args
        self.joint_state: JointState | None = None
        self.wrench: WrenchStamped | None = None
        self.create_subscription(JointState, args.joint_state_topic, self._on_joint_state, 10)
        if not args.skip_force_check:
            self.create_subscription(WrenchStamped, args.force_topic, self._on_wrench, 10)
        self.action_client = ActionClient(self, FollowJointTrajectory, args.action_name)

    def _on_joint_state(self, msg: JointState) -> None:
        self.joint_state = msg

    def _on_wrench(self, msg: WrenchStamped) -> None:
        self.wrench = msg

    def wait_for_joint_state(self) -> JointState:
        deadline = time.monotonic() + self.args.wait_s
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.1)
            if self.joint_state is not None and all(name in self.joint_state.name for name in JOINT_NAMES):
                return self.joint_state
        raise RuntimeError(f"Timed out waiting for joint state on {self.args.joint_state_topic}")

    def wait_for_force_gate(self) -> dict[str, Any] | None:
        if self.args.skip_force_check:
            return None
        deadline = time.monotonic() + self.args.wait_s
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.1)
            if self.wrench is not None:
                force = self.wrench.wrench.force
                force_norm = math.sqrt(force.x * force.x + force.y * force.y + force.z * force.z)
                if force_norm > self.args.max_force_n:
                    raise RuntimeError(
                        f"Force gate blocked motion: {force_norm:.3f} N > {self.args.max_force_n:.3f} N"
                    )
                return {
                    "topic": self.args.force_topic,
                    "force_n": {"x": force.x, "y": force.y, "z": force.z},
                    "force_norm_n": force_norm,
                    "max_force_n": self.args.max_force_n,
                }
        raise RuntimeError(f"Timed out waiting for force sample on {self.args.force_topic}")

    def run(self) -> dict[str, Any]:
        dashboard = dashboard_exchange(self.args.robot_ip, ["is in remote control", "safetymode", "robotmode", "running"])
        _validate_dashboard(dashboard)
        joint_state = self.wait_for_joint_state()
        force_gate = self.wait_for_force_gate()
        positions = _ordered_positions(joint_state)
        if not self.action_client.wait_for_server(timeout_sec=self.args.wait_s):
            raise RuntimeError(f"Action server unavailable: {self.args.action_name}")

        goal = FollowJointTrajectory.Goal()
        goal.trajectory.joint_names = JOINT_NAMES
        goal.trajectory.points = _trajectory_points(positions, self.args.joint_name, self.args.delta_rad, self.args.duration_s)

        summary = {
            "execute": bool(self.args.execute),
            "robot_ip": self.args.robot_ip,
            "action_name": self.args.action_name,
            "joint_name": self.args.joint_name,
            "delta_rad": self.args.delta_rad,
            "duration_s": self.args.duration_s,
            "dashboard": dashboard,
            "force_gate": force_gate,
            "start_positions": dict(zip(JOINT_NAMES, positions)),
            "sent_goal": False,
            "accepted": False,
            "result_status": None,
        }
        if not self.args.execute:
            return summary

        send_future = self.action_client.send_goal_async(goal)
        rclpy.spin_until_future_complete(self, send_future, timeout_sec=self.args.wait_s)
        goal_handle = send_future.result()
        if goal_handle is None:
            raise RuntimeError("FollowJointTrajectory goal did not return a handle")
        summary["sent_goal"] = True
        summary["accepted"] = bool(goal_handle.accepted)
        if not goal_handle.accepted:
            raise RuntimeError("FollowJointTrajectory goal was rejected")
        result_future = goal_handle.get_result_async()
        rclpy.spin_until_future_complete(self, result_future, timeout_sec=self.args.duration_s + self.args.wait_s)
        result = result_future.result()
        if result is None:
            raise RuntimeError("Timed out waiting for FollowJointTrajectory result")
        summary["result_status"] = int(result.status)
        summary["result_error_code"] = int(result.result.error_code)
        summary["result_error_string"] = str(result.result.error_string)
        if result.result.error_code != FollowJointTrajectory.Result.SUCCESSFUL:
            raise RuntimeError(f"Trajectory failed: {result.result.error_code} {result.result.error_string}")
        return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a minimal UR10e no-contact joint trajectory probe.")
    parser.add_argument("--execute", action="store_true", help="Actually send the trajectory goal.")
    parser.add_argument("--robot-ip", default="192.168.1.18")
    parser.add_argument("--action-name", default="/scaled_joint_trajectory_controller/follow_joint_trajectory")
    parser.add_argument("--joint-state-topic", default="/joint_states")
    parser.add_argument("--joint-name", default="wrist_3_joint", choices=JOINT_NAMES)
    parser.add_argument("--delta-rad", type=float, default=0.01)
    parser.add_argument("--duration-s", type=float, default=6.0)
    parser.add_argument("--wait-s", type=float, default=15.0)
    parser.add_argument("--force-topic", default="/force_torque_sensor_broadcaster/ft_data")
    parser.add_argument("--max-force-n", type=float, default=10.0)
    parser.add_argument("--skip-force-check", action="store_true")
    parser.add_argument("--summary", type=Path, default=None)
    args = parser.parse_args(argv)

    rclpy.init(args=None)
    node = NoContactMotionProbe(args)
    try:
        summary = node.run()
        payload = json.dumps(summary, indent=2, sort_keys=True) + "\n"
        if args.summary is not None:
            args.summary.parent.mkdir(parents=True, exist_ok=True)
            args.summary.write_text(payload, encoding="utf-8")
        print(payload, end="")
        return 0
    except Exception as exc:
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        if args.summary is not None:
            args.summary.parent.mkdir(parents=True, exist_ok=True)
            args.summary.write_text(text, encoding="utf-8")
        print(text, end="", file=sys.stderr)
        return 2
    finally:
        node.destroy_node()
        rclpy.shutdown()


def _validate_dashboard(responses: dict[str, str]) -> None:
    if responses.get("is in remote control") != "true":
        raise RuntimeError(f"Remote Control is not true: {responses.get('is in remote control')}")
    if "NORMAL" not in responses.get("safetymode", ""):
        raise RuntimeError(f"Safety mode is not NORMAL: {responses.get('safetymode')}")
    if "RUNNING" not in responses.get("robotmode", ""):
        raise RuntimeError(f"Robot mode is not RUNNING: {responses.get('robotmode')}")
    if responses.get("running") != "Program running: false":
        raise RuntimeError(f"A program is already running: {responses.get('running')}")


def _ordered_positions(joint_state: JointState) -> list[float]:
    by_name = {name: joint_state.position[index] for index, name in enumerate(joint_state.name)}
    return [float(by_name[name]) for name in JOINT_NAMES]


def _trajectory_points(start: list[float], joint_name: str, delta_rad: float, duration_s: float) -> list[JointTrajectoryPoint]:
    if duration_s <= 0.0 or not math.isfinite(duration_s):
        raise ValueError("duration_s must be positive")
    if abs(delta_rad) > 0.03:
        raise ValueError("delta_rad is capped at 0.03 rad for this no-contact probe")
    mid = list(start)
    mid[JOINT_NAMES.index(joint_name)] += delta_rad
    return [
        _point(start, duration_s * 0.25),
        _point(mid, duration_s * 0.60),
        _point(start, duration_s),
    ]


def _point(positions: list[float], time_from_start_s: float) -> JointTrajectoryPoint:
    point = JointTrajectoryPoint()
    point.positions = positions
    point.velocities = [0.0] * len(positions)
    point.time_from_start.sec = int(time_from_start_s)
    point.time_from_start.nanosec = int((time_from_start_s - int(time_from_start_s)) * 1_000_000_000)
    return point


if __name__ == "__main__":
    raise SystemExit(main())
