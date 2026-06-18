from __future__ import annotations

import argparse
import csv
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from . import step56_simulation_matrix as offline
from .step5a_cartesian_cycloid_motion import (
    DEFAULT_CALIBRATION_YAML,
    DEFAULT_XACRO_PATH,
    JOINT_NAMES,
)


WORKSPACE = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CONTROLLERS_YAML = PACKAGE_ROOT / "config" / "gazebo_matrix_controllers.yaml"
INITIAL_POSITIONS_YAML = PACKAGE_ROOT / "config" / "gazebo_matrix_initial_positions.yaml"
ACTION_NAME = "/joint_trajectory_controller/follow_joint_trajectory"


@dataclass(frozen=True)
class ReferencePoint:
    t_s: float
    x_m: float
    y_m: float
    z_m: float
    segment: str
    reference_speed_m_s: float


def generate_sim_robot_description(
    *,
    controllers_yaml: Path = CONTROLLERS_YAML,
    initial_positions_yaml: Path = INITIAL_POSITIONS_YAML,
    calibration_yaml: Path = DEFAULT_CALIBRATION_YAML,
    xacro_path: Path = DEFAULT_XACRO_PATH,
) -> str:
    import xacro

    if not controllers_yaml.exists():
        raise FileNotFoundError(f"controller YAML not found: {controllers_yaml}")
    if not initial_positions_yaml.exists():
        raise FileNotFoundError(f"initial positions YAML not found: {initial_positions_yaml}")
    if not calibration_yaml.exists():
        raise FileNotFoundError(f"calibration YAML not found: {calibration_yaml}")
    if not xacro_path.exists():
        raise FileNotFoundError(f"UR xacro not found: {xacro_path}")

    doc = xacro.process_file(
        str(xacro_path),
        mappings={
            "name": "ur",
            "ur_type": "ur10e",
            "robot_ip": "192.168.1.18",
            "kinematics_params": str(calibration_yaml),
            "sim_ignition": "true",
            "simulation_controllers": str(controllers_yaml),
            "initial_positions_file": str(initial_positions_yaml),
        },
    )
    robot_description = doc.toxml()
    if "ign_ros2_control/IgnitionSystem" not in robot_description:
        raise RuntimeError("generated URDF is missing ign_ros2_control/IgnitionSystem")
    if "ur_robot_driver/URPositionHardwareInterface" in robot_description:
        raise RuntimeError("generated URDF still contains real URPositionHardwareInterface")
    if "libign_ros2_control-system.so" not in robot_description:
        raise RuntimeError("generated URDF is missing libign_ros2_control-system.so plugin")
    return robot_description


def load_initial_positions(initial_positions_yaml: Path = INITIAL_POSITIONS_YAML) -> list[float]:
    payload = yaml.safe_load(initial_positions_yaml.read_text(encoding="utf-8"))
    return [float(payload[name]) for name in JOINT_NAMES]


def _as_reference_rows_from_trajectory(artifact: dict[str, Any]) -> list[ReferencePoint]:
    rows = []
    fallback_z = 0.2273
    for row in artifact["trajectory"]["rows"]:
        base_xy = row["base_xy_m"]
        z_m = row.get("base_z_m")
        rows.append(
            ReferencePoint(
                t_s=float(row["t_s"]),
                x_m=float(base_xy[0]),
                y_m=float(base_xy[1]),
                z_m=float(z_m) if z_m is not None else fallback_z,
                segment="reference",
                reference_speed_m_s=float(row.get("reference_speed_m_s", 0.0)),
            )
        )
    return rows


def _step5b_reference_rows() -> list[ReferencePoint]:
    artifact = offline.build_stage_artifact("step5b")
    rows: list[ReferencePoint] = []
    for row in artifact["preposition"]["rows"]:
        rows.append(
            ReferencePoint(
                t_s=float(row["t_s"]),
                x_m=float(row["tcp_x_m"]),
                y_m=float(row["tcp_y_m"]),
                z_m=float(row["tcp_z_m"]),
                segment="step5b_preposition",
                reference_speed_m_s=float(row.get("velocity_norm_m_s", 0.0)),
            )
        )

    spec = offline.STAGE_REGISTRY["step5b"]
    stage = offline._stage_from_table(spec.stage_table_path, spec.source_stage_id)
    safe_frame = offline.load_json(spec.safe_frame_path)
    t_offset = rows[-1].t_s if rows else 0.0
    z_m = float(artifact["preposition"]["target_xyz_m"][2])
    for t_s, local_xy, local_vxy in offline._sample_cycloid(stage):
        base_xy = offline._rotation_map(safe_frame, local_xy)
        rows.append(
            ReferencePoint(
                t_s=t_offset + float(t_s),
                x_m=float(base_xy[0]),
                y_m=float(base_xy[1]),
                z_m=z_m,
                segment="step5b_cycloid",
                reference_speed_m_s=math.hypot(float(local_vxy[0]), float(local_vxy[1])),
            )
        )
    return rows


def build_reference_rows(stage_id: str) -> list[ReferencePoint]:
    if stage_id == "step5b":
        return _step5b_reference_rows()
    artifact = offline.build_stage_artifact(stage_id)
    return _as_reference_rows_from_trajectory(artifact)


def build_command_trace(
    stage_id: str,
    output_dir: Path,
    *,
    initial_positions_yaml: Path = INITIAL_POSITIONS_YAML,
) -> tuple[Path, dict[str, Any], list[list[float]]]:
    import numpy as np
    import pinocchio as pin

    from .step5a_cartesian_cycloid_motion import build_calibrated_model, fk_tool0_base, solve_tool0_ik

    output_dir.mkdir(parents=True, exist_ok=True)
    model_bundle = build_calibrated_model()
    q = np.array(load_initial_positions(initial_positions_yaml), dtype=float)
    anchor_rotation = fk_tool0_base(model_bundle, q).rotation.copy()
    references = build_reference_rows(stage_id)
    command_rows: list[dict[str, Any]] = []
    joint_points: list[list[float]] = []
    max_error_m = 0.0
    max_reference_speed = 0.0

    for ref in references:
        target = pin.SE3(anchor_rotation, np.array([ref.x_m, ref.y_m, ref.z_m], dtype=float))
        q, ik_error_m = solve_tool0_ik(
            model_bundle,
            q,
            target,
            damping=1e-4,
            max_iters=300,
            tolerance_m=1e-5,
        )
        fk = fk_tool0_base(model_bundle, q)
        fk_xyz = [float(value) for value in fk.translation]
        cartesian_error_m = float(np.linalg.norm(fk.translation - target.translation))
        max_error_m = max(max_error_m, cartesian_error_m, float(ik_error_m))
        max_reference_speed = max(max_reference_speed, ref.reference_speed_m_s)
        joint_values = [float(value) for value in q.tolist()]
        joint_points.append(joint_values)
        row = {
            "stage_id": stage_id,
            "t_s": ref.t_s,
            "segment": ref.segment,
            "reference_x_m": ref.x_m,
            "reference_y_m": ref.y_m,
            "reference_z_m": ref.z_m,
            "reference_speed_m_s": ref.reference_speed_m_s,
            "commanded_fk_x_m": fk_xyz[0],
            "commanded_fk_y_m": fk_xyz[1],
            "commanded_fk_z_m": fk_xyz[2],
            "cartesian_error_m": cartesian_error_m,
        }
        for name, value in zip(JOINT_NAMES, joint_values):
            row[f"command_{name}_rad"] = value
        command_rows.append(row)

    trace_path = output_dir / "command_trace.csv"
    fieldnames = [
        "stage_id",
        "t_s",
        "segment",
        "reference_x_m",
        "reference_y_m",
        "reference_z_m",
        "reference_speed_m_s",
        "commanded_fk_x_m",
        "commanded_fk_y_m",
        "commanded_fk_z_m",
        "cartesian_error_m",
        *[f"command_{name}_rad" for name in JOINT_NAMES],
    ]
    with trace_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(command_rows)

    duration_s = float(command_rows[-1]["t_s"]) if command_rows else 0.0
    metrics = {
        "sample_count": len(command_rows),
        "duration_s": duration_s,
        "max_commanded_ik_error_m": max_error_m,
        "max_reference_speed_m_s": max_reference_speed,
        "trace_path": str(trace_path),
        "first_reference_xyz_m": [
            command_rows[0]["reference_x_m"],
            command_rows[0]["reference_y_m"],
            command_rows[0]["reference_z_m"],
        ]
        if command_rows
        else None,
        "final_reference_xyz_m": [
            command_rows[-1]["reference_x_m"],
            command_rows[-1]["reference_y_m"],
            command_rows[-1]["reference_z_m"],
        ]
        if command_rows
        else None,
    }
    return trace_path, metrics, joint_points


def execute_joint_trajectory(
    joint_points: list[list[float]],
    *,
    duration_s: float,
    action_name: str = ACTION_NAME,
    joint_state_topic: str = "/joint_states",
    server_timeout_s: float = 10.0,
    result_timeout_s: float | None = None,
) -> dict[str, Any]:
    import rclpy
    from builtin_interfaces.msg import Duration
    from control_msgs.action import FollowJointTrajectory
    from rclpy.action import ActionClient
    from rclpy.node import Node
    from sensor_msgs.msg import JointState
    from trajectory_msgs.msg import JointTrajectoryPoint

    class RunnerNode(Node):
        def __init__(self) -> None:
            super().__init__("ur10e_gazebo_matrix_runner")
            self.samples: list[list[float]] = []
            self.client = ActionClient(self, FollowJointTrajectory, action_name)
            self.create_subscription(JointState, joint_state_topic, self._on_joint_state, 10)

        def _on_joint_state(self, msg: JointState) -> None:
            by_name = dict(zip(msg.name, msg.position))
            if all(name in by_name for name in JOINT_NAMES):
                self.samples.append([float(by_name[name]) for name in JOINT_NAMES])

    if not joint_points:
        raise RuntimeError("joint trajectory has no points")
    rclpy.init()
    node = RunnerNode()
    try:
        deadline = time.monotonic() + server_timeout_s
        while not node.client.wait_for_server(timeout_sec=0.25):
            rclpy.spin_once(node, timeout_sec=0.05)
            if time.monotonic() > deadline:
                return {
                    "ok": False,
                    "action_accepted": False,
                    "result_status": None,
                    "result_error_code": None,
                    "observed_joint_state_samples": len(node.samples),
                    "observed_motion": False,
                    "blocker": "action_server_unavailable",
                    "action_name": action_name,
                }

        goal = FollowJointTrajectory.Goal()
        goal.trajectory.joint_names = JOINT_NAMES
        count = len(joint_points)
        for index, positions in enumerate(joint_points):
            point = JointTrajectoryPoint()
            point.positions = positions
            t_s = duration_s * index / max(count - 1, 1)
            point.time_from_start = Duration(sec=int(t_s), nanosec=int((t_s % 1.0) * 1_000_000_000))
            goal.trajectory.points.append(point)

        send_future = node.client.send_goal_async(goal)
        rclpy.spin_until_future_complete(node, send_future, timeout_sec=server_timeout_s)
        handle = send_future.result()
        if handle is None or not handle.accepted:
            return {
                "ok": False,
                "action_accepted": bool(handle and handle.accepted),
                "result_status": None,
                "result_error_code": None,
                "observed_joint_state_samples": len(node.samples),
                "observed_motion": False,
                "blocker": "action_goal_rejected",
                "action_name": action_name,
            }
        result_future = handle.get_result_async()
        timeout = result_timeout_s if result_timeout_s is not None else max(duration_s + 10.0, 30.0)
        rclpy.spin_until_future_complete(node, result_future, timeout_sec=timeout)
        if not result_future.done():
            return {
                "ok": False,
                "action_accepted": True,
                "result_status": None,
                "result_error_code": None,
                "observed_joint_state_samples": len(node.samples),
                "observed_motion": _observed_motion(node.samples),
                "blocker": "action_result_timeout",
                "action_name": action_name,
            }
        wrapped = result_future.result()
        error_code = int(wrapped.result.error_code)
        return {
            "ok": error_code == FollowJointTrajectory.Result.SUCCESSFUL,
            "action_accepted": True,
            "result_status": int(wrapped.status),
            "result_error_code": error_code,
            "result_error_string": str(wrapped.result.error_string),
            "observed_joint_state_samples": len(node.samples),
            "observed_motion": _observed_motion(node.samples),
            "blocker": None if error_code == FollowJointTrajectory.Result.SUCCESSFUL else "action_result_error",
            "action_name": action_name,
        }
    finally:
        node.destroy_node()
        rclpy.shutdown()


def _observed_motion(samples: list[list[float]], threshold_rad: float = 1e-4) -> bool:
    if len(samples) < 2:
        return False
    first = samples[0]
    return any(abs(value - first[index]) > threshold_rad for sample in samples[1:] for index, value in enumerate(sample))


def write_stage_summary(
    stage_id: str,
    output_dir: Path,
    *,
    execute: bool = False,
    screenshot_path: Path | None = None,
) -> Path:
    stage_dir = output_dir / stage_id
    trace_path, metrics, joint_points = build_command_trace(stage_id, stage_dir)
    execution = None
    if execute:
        execution = execute_joint_trajectory(joint_points, duration_s=float(metrics["duration_s"]))
    artifact = offline.build_stage_artifact(stage_id)
    summary = {
        "schema": "ur10e_gazebo_matrix_stage_result_v1",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "stage_id": stage_id,
        "mode": "gazebo_ros2_control",
        "gazebo_only": True,
        "live_robot_command_authorized": False,
        "force_physics_closed_loop": False,
        "controller_required": True,
        "action_name": ACTION_NAME,
        "execute_requested": execute,
        "execution": execution,
        "metrics": metrics,
        "trace_path": str(trace_path),
        "screenshot_path": str(screenshot_path) if screenshot_path else None,
        "source_paths": artifact.get("source_paths", {}),
        "frames": artifact.get("frames", {}),
        "units": artifact.get("units", {}),
        "known_limit": artifact.get("known_blocker"),
        "acceptance": {
            "trajectory_duration_nonzero": float(metrics["duration_s"]) > 0.0,
            "trace_written": trace_path.is_file(),
            "max_tcp_xy_path_error_limit_m": 0.005 if stage_id not in {"step5c", "step5d"} else 0.010,
            "max_commanded_ik_error_m": metrics["max_commanded_ik_error_m"],
            "ik_error_within_limit": metrics["max_commanded_ik_error_m"]
            <= (0.005 if stage_id not in {"step5c", "step5d"} else 0.010),
            "action_success": bool(execution and execution.get("ok")) if execute else None,
            "observed_motion": bool(execution and execution.get("observed_motion")) if execute else None,
            "gui_evidence_captured": bool(screenshot_path and screenshot_path.exists()),
        },
    }
    summary_path = stage_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary_path


def run_matrix(
    stage: str,
    output_dir: Path,
    *,
    execute: bool = False,
    screenshot_path: Path | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stage_ids = list(offline.STAGE_REGISTRY) if stage == "all" else [stage]
    if stage != "all" and stage not in offline.STAGE_REGISTRY:
        raise SystemExit(f"unknown stage {stage!r}; expected all or one of {', '.join(offline.STAGE_REGISTRY)}")
    summaries = [write_stage_summary(stage_id, output_dir, execute=execute, screenshot_path=screenshot_path) for stage_id in stage_ids]
    payload = {
        "schema": "ur10e_gazebo_matrix_result_v1",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "gazebo_ros2_control",
        "execute_requested": execute,
        "gazebo_only": True,
        "live_robot_command_authorized": False,
        "stage_count": len(summaries),
        "controller_required": True,
        "action_name": ACTION_NAME,
        "stages": [json.loads(path.read_text(encoding="utf-8")) for path in summaries],
    }
    path = output_dir / "matrix_summary.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run or dry-plan UR10e Step5/Step6 Gazebo ros2_control matrix.")
    parser.add_argument("--stage", default="all", choices=["all", *offline.STAGE_REGISTRY.keys()])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--execute", action="store_true", help="Send trajectories to the Gazebo joint trajectory action.")
    parser.add_argument("--screenshot-path", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = run_matrix(args.stage, args.output_dir, execute=args.execute, screenshot_path=args.screenshot_path)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
