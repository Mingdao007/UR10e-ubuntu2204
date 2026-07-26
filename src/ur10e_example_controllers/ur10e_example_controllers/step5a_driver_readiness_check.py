from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import rclpy
from controller_manager_msgs.srv import ListControllers
from rclpy.node import Node
from sensor_msgs.msg import JointState


JOINT_NAMES = [
    "shoulder_pan_joint",
    "shoulder_lift_joint",
    "elbow_joint",
    "wrist_1_joint",
    "wrist_2_joint",
    "wrist_3_joint",
]

FATAL_LOG_PATTERN = re.compile(
    r"Could not get configuration package|\[ur_ros2_control_node-[0-9]+\].*\[FATAL\]|"
    r"Failed to set the initial state|\[ERROR\] \[ur_ros2_control_node-[0-9]+\]: process has died"
)


class DriverReadinessCheck(Node):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__("step5a_driver_readiness_check")
        self.args = args
        self.latest_joint_state: JointState | None = None
        self.controllers: dict[str, str] = {}
        self.create_subscription(JointState, args.joint_state_topic, self._on_joint_state, 10)
        self.controller_client = self.create_client(ListControllers, args.controller_manager + "/list_controllers")

    def _on_joint_state(self, msg: JointState) -> None:
        self.latest_joint_state = msg

    def run(self) -> dict[str, Any]:
        deadline = time.monotonic() + self.args.timeout_s
        last_controller_error: str | None = None
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.05)
            fatal_lines = _fatal_lines(self.args.launch_log)
            if fatal_lines:
                return self._summary(False, "driver_fatal_log_detected", fatal_lines, last_controller_error)

            log_ok = _hardware_activate_seen(self.args.launch_log)
            joint_ok = self._joint_state_ok()
            controllers_ok = False
            if self.controller_client.wait_for_service(timeout_sec=0.05):
                future = self.controller_client.call_async(ListControllers.Request())
                rclpy.spin_until_future_complete(self, future, timeout_sec=0.5)
                if future.done() and future.result() is not None:
                    self.controllers = {controller.name: controller.state for controller in future.result().controller}
                    controllers_ok = self._controllers_ok()
                elif future.done() and future.exception() is not None:
                    last_controller_error = repr(future.exception())

            if log_ok and joint_ok and controllers_ok:
                return self._summary(True, "driver_ready", [], last_controller_error)

        return self._summary(False, "timed_out_waiting_for_driver_readiness", _fatal_lines(self.args.launch_log), last_controller_error)

    def _joint_state_ok(self) -> bool:
        return self.latest_joint_state is not None and all(name in self.latest_joint_state.name for name in JOINT_NAMES)

    def _controllers_ok(self) -> bool:
        required = ["joint_state_broadcaster"]
        if self.args.require_scaled_controller:
            required.append("scaled_joint_trajectory_controller")
        return all(self.controllers.get(name) == "active" for name in required)

    def _summary(
        self,
        ok: bool,
        status: str,
        fatal_lines: list[str],
        last_controller_error: str | None,
    ) -> dict[str, Any]:
        joint_names = [] if self.latest_joint_state is None else list(self.latest_joint_state.name)
        return {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "role": self.args.role,
            "ok": ok,
            "status": status,
            "run_dir": str(self.args.run_dir) if self.args.run_dir else None,
            "launch_log": str(self.args.launch_log),
            "sent_goal": False,
            "hardware_activate_log_seen": _hardware_activate_seen(self.args.launch_log),
            "joint_state_broadcaster_active": self.controllers.get("joint_state_broadcaster") == "active",
            "scaled_joint_trajectory_controller_active": self.controllers.get("scaled_joint_trajectory_controller") == "active",
            "require_scaled_controller": bool(self.args.require_scaled_controller),
            "joint_states_have_ur10e_six_joints": all(name in joint_names for name in JOINT_NAMES),
            "joint_state_names": joint_names,
            "controllers": self.controllers,
            "driver_fatal_grep": fatal_lines,
            "last_controller_error": last_controller_error,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a sustained UR10e ROS2 driver launch before Step5a motion.")
    parser.add_argument("--launch-log", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--controllers-log", type=Path, default=None)
    parser.add_argument("--joint-states-log", type=Path, default=None)
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument("--timeout-s", type=float, default=30.0)
    parser.add_argument("--controller-manager", default="/controller_manager")
    parser.add_argument("--joint-state-topic", default="/joint_states")
    parser.add_argument("--role", default="step5a_driver_readiness_gate")
    parser.add_argument("--allow-inactive-scaled-controller", action="store_true")
    args = parser.parse_args(argv)
    args.require_scaled_controller = not args.allow_inactive_scaled_controller

    rclpy.init(args=None)
    node = DriverReadinessCheck(args)
    try:
        summary = node.run()
    finally:
        node.destroy_node()
        rclpy.shutdown()

    _write_outputs(args, summary)
    text = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    print(text, end="" if summary["ok"] else "", file=sys.stdout if summary["ok"] else sys.stderr)
    return 0 if summary["ok"] else 2


def _write_outputs(args: argparse.Namespace, summary: dict[str, Any]) -> None:
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.controllers_log is not None:
        args.controllers_log.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"{name}\t{state}" for name, state in sorted(summary["controllers"].items())]
        args.controllers_log.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    if args.joint_states_log is not None:
        args.joint_states_log.parent.mkdir(parents=True, exist_ok=True)
        args.joint_states_log.write_text("\n".join(summary["joint_state_names"]) + "\n", encoding="utf-8")


def _hardware_activate_seen(path: Path) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    return "Successful 'activate' of hardware 'ur10e'" in text


def _fatal_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    lines = []
    for index, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        if FATAL_LOG_PATTERN.search(line):
            lines.append(f"{index}:{line}")
    return lines[-12:]


if __name__ == "__main__":
    raise SystemExit(main())
