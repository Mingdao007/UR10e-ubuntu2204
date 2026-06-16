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
        self.force_samples: list[tuple[float, float, float]] = []
        self.create_subscription(JointState, args.joint_state_topic, self._on_joint_state, 10)
        if not args.skip_force_check:
            self.create_subscription(WrenchStamped, args.force_topic, self._on_wrench, 10)
        self.action_client = ActionClient(self, FollowJointTrajectory, args.action_name)

    def _on_joint_state(self, msg: JointState) -> None:
        self.joint_state = msg

    def _on_wrench(self, msg: WrenchStamped) -> None:
        self.wrench = msg
        self.force_samples.append(_force_vector(msg))
        self.force_samples = self.force_samples[-1000:]

    def wait_for_joint_state(self) -> JointState:
        deadline = time.monotonic() + self.args.wait_s
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.1)
            if self.joint_state is not None and all(name in self.joint_state.name for name in JOINT_NAMES):
                return self.joint_state
        raise RuntimeError(f"Timed out waiting for joint state on {self.args.joint_state_topic}")

    def collect_force_baseline(self) -> dict[str, Any] | None:
        if self.args.skip_force_check:
            return None
        deadline = time.monotonic() + self.args.wait_s
        baseline_until: float | None = None
        self.force_samples.clear()
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.1)
            sample_count = len(self.force_samples)
            if sample_count > 0 and baseline_until is None:
                baseline_until = time.monotonic() + self.args.force_baseline_s
            if (
                baseline_until is not None
                and time.monotonic() >= baseline_until
                and sample_count >= self.args.min_force_samples
            ):
                baseline = _mean_vector(self.force_samples)
                force_norm = _norm(baseline)
                return {
                    "topic": self.args.force_topic,
                    "baseline_force_n": _vector_payload(baseline),
                    "baseline_force_norm_n": force_norm,
                    "baseline_sample_count": sample_count,
                    "baseline_s": self.args.force_baseline_s,
                    "max_force_delta_n": self.args.max_force_delta_n,
                    "max_observed_force_delta_n": 0.0,
                    "blocked": False,
                }
        raise RuntimeError(f"Timed out waiting for force sample on {self.args.force_topic}")

    def force_delta(self, force_gate: dict[str, Any] | None) -> float:
        if self.args.skip_force_check or force_gate is None or self.wrench is None:
            return 0.0
        baseline = _payload_vector(force_gate["baseline_force_n"])
        return _norm(_vector_sub(_force_vector(self.wrench), baseline))

    def run(self) -> dict[str, Any]:
        dashboard = dashboard_exchange(self.args.robot_ip, ["is in remote control", "safetymode", "robotmode", "running"])
        _validate_dashboard(dashboard)
        joint_state = self.wait_for_joint_state()
        force_gate = self.collect_force_baseline()
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
        deadline = time.monotonic() + self.args.duration_s + self.args.wait_s
        while rclpy.ok() and time.monotonic() < deadline and not result_future.done():
            rclpy.spin_once(self, timeout_sec=0.05)
            force_delta = self.force_delta(force_gate)
            if force_gate is not None:
                force_gate["max_observed_force_delta_n"] = max(
                    force_gate["max_observed_force_delta_n"],
                    force_delta,
                )
            if force_delta > self.args.max_force_delta_n:
                if force_gate is not None:
                    force_gate["blocked"] = True
                    force_gate["block_force_delta_n"] = force_delta
                cancel_future = goal_handle.cancel_goal_async()
                rclpy.spin_until_future_complete(self, cancel_future, timeout_sec=1.0)
                raise RuntimeError(
                    f"Force delta gate blocked motion: {force_delta:.3f} N > {self.args.max_force_delta_n:.3f} N"
                )
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
    parser.add_argument("--force-baseline-s", type=float, default=1.0)
    parser.add_argument("--min-force-samples", type=int, default=5)
    parser.add_argument("--max-force-delta-n", type=float, default=8.0)
    parser.add_argument("--max-force-n", type=float, default=None, help="Compatibility alias for --max-force-delta-n.")
    parser.add_argument("--skip-force-check", action="store_true")
    parser.add_argument("--summary", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.max_force_n is not None:
        args.max_force_delta_n = args.max_force_n

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
    running = responses.get("running")
    if running not in {"Program running: false", "Program running: true"}:
        raise RuntimeError(f"Unexpected Dashboard running state: {running}")


def _ordered_positions(joint_state: JointState) -> list[float]:
    by_name = {name: joint_state.position[index] for index, name in enumerate(joint_state.name)}
    return [float(by_name[name]) for name in JOINT_NAMES]


def _force_vector(msg: WrenchStamped) -> tuple[float, float, float]:
    force = msg.wrench.force
    return (float(force.x), float(force.y), float(force.z))


def _mean_vector(samples: list[tuple[float, float, float]]) -> tuple[float, float, float]:
    width = float(len(samples))
    return (
        sum(sample[0] for sample in samples) / width,
        sum(sample[1] for sample in samples) / width,
        sum(sample[2] for sample in samples) / width,
    )


def _vector_sub(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _norm(vector: tuple[float, float, float]) -> float:
    return math.sqrt(vector[0] * vector[0] + vector[1] * vector[1] + vector[2] * vector[2])


def _vector_payload(vector: tuple[float, float, float]) -> dict[str, float]:
    return {"x": vector[0], "y": vector[1], "z": vector[2]}


def _payload_vector(payload: dict[str, float]) -> tuple[float, float, float]:
    return (float(payload["x"]), float(payload["y"]), float(payload["z"]))


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
