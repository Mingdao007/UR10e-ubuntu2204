from __future__ import annotations

import argparse
import csv
import json
import math
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from ament_index_python.packages import PackageNotFoundError, get_package_share_directory

from . import step56_simulation_matrix as offline
from .step5a_cartesian_cycloid_motion import (
    DEFAULT_CALIBRATION_YAML,
    DEFAULT_XACRO_PATH,
    JOINT_NAMES,
)


def _workspace_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if parent.name == "ur10e_ros2_ws":
            return parent
    return Path(__file__).resolve().parents[3]


def _package_root() -> Path:
    try:
        return Path(get_package_share_directory("ur10e_example_controllers"))
    except PackageNotFoundError:
        return Path(__file__).resolve().parents[1]


WORKSPACE = _workspace_root()
PACKAGE_ROOT = _package_root()
CONTROLLERS_YAML = PACKAGE_ROOT / "config" / "gazebo_matrix_controllers.yaml"
INITIAL_POSITIONS_YAML = PACKAGE_ROOT / "config" / "gazebo_matrix_initial_positions.yaml"
ACTION_NAME = "/joint_trajectory_controller/follow_joint_trajectory"
CONTACT_STAGE_IDS = frozenset({"step5b", "step5d", "step6b", "step7", "step8"})
CONTACT_SURFACE_Z_M = 0.008044839
DEFAULT_TARGET_LOAD_N = 5.0
DEFAULT_CONTACT_STIFFNESS_N_M = 2500.0
TCP_VISUAL_LINK = "tool0_tcp_visual_marker"
TCP_VISUAL_JOINT = "tool0_tcp_visual_marker_joint"
EOAT_VISUAL_LINK = "real_aligned_eoat_visual_stack"
EOAT_VISUAL_JOINT = "real_aligned_eoat_visual_stack_joint"
ACTIVE_TCP_OFFSET_TOOL0_M = (
    0.0000018186503701174852,
    0.00000022293003722353485,
    0.12209917288991741,
)
ACTION_RESULT_TIMEOUT_MIN_S = 60.0
ACTION_RESULT_TIMEOUT_SLOWDOWN_FACTOR = 2.0
ACTION_RESULT_TIMEOUT_EXTRA_S = 10.0


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
    return add_tcp_visual_marker(add_real_aligned_eoat_visual_stack(robot_description))


def add_real_aligned_eoat_visual_stack(robot_description: str) -> str:
    """Attach a non-colliding visual proxy for the real end-of-arm tooling stack."""

    root = ET.fromstring(robot_description)
    if root.find(f"./link[@name='{EOAT_VISUAL_LINK}']") is not None:
        return robot_description
    tool0 = root.find("./link[@name='tool0']")
    if tool0 is None:
        raise RuntimeError("generated URDF is missing tool0 link for EOAT visual stack")

    link = ET.Element("link", {"name": EOAT_VISUAL_LINK})
    _append_eoat_visual(
        link,
        name="eoat_flange_adapter_visual",
        xyz=(0.0, 0.0, 0.010),
        rpy="0 0 0",
        geometry_kind="cylinder",
        geometry_attrs={"radius": "0.048", "length": "0.020"},
        rgba="0.40 0.42 0.44 1.0",
    )
    _append_eoat_visual(
        link,
        name="eoat_kunwei_sensor_body_visual",
        xyz=(0.0, 0.0, 0.044),
        rpy="0 0 0",
        geometry_kind="cylinder",
        geometry_attrs={"radius": "0.034", "length": "0.048"},
        rgba="0.05 0.22 0.30 1.0",
    )
    _append_eoat_visual(
        link,
        name="eoat_tool_plate_visual",
        xyz=(0.0, 0.0, 0.075),
        rpy="0 0 0",
        geometry_kind="box",
        geometry_attrs={"size": "0.076 0.046 0.012"},
        rgba="0.72 0.72 0.68 1.0",
    )
    _append_eoat_visual(
        link,
        name="eoat_contact_tip_visual",
        xyz=(0.0, 0.0, 0.100),
        rpy="0 0 0",
        geometry_kind="cylinder",
        geometry_attrs={"radius": "0.007", "length": "0.044"},
        rgba="0.95 0.76 0.18 1.0",
    )
    _append_eoat_visual(
        link,
        name="eoat_active_tcp_marker_visual",
        xyz=ACTIVE_TCP_OFFSET_TOOL0_M,
        rpy="0 0 0",
        geometry_kind="sphere",
        geometry_attrs={"radius": "0.012"},
        rgba="1.0 0.0 1.0 1.0",
    )

    joint = ET.Element("joint", {"name": EOAT_VISUAL_JOINT, "type": "fixed"})
    ET.SubElement(joint, "parent", {"link": "tool0"})
    ET.SubElement(joint, "child", {"link": EOAT_VISUAL_LINK})
    ET.SubElement(joint, "origin", {"xyz": "0 0 0", "rpy": "0 0 0"})

    root.append(link)
    root.append(joint)
    return ET.tostring(root, encoding="unicode")


def _append_eoat_visual(
    link: ET.Element,
    *,
    name: str,
    xyz: tuple[float, float, float],
    rpy: str,
    geometry_kind: str,
    geometry_attrs: dict[str, str],
    rgba: str,
) -> None:
    visual = ET.SubElement(link, "visual", {"name": name})
    ET.SubElement(visual, "origin", {"xyz": _xyz(xyz), "rpy": rpy})
    geometry = ET.SubElement(visual, "geometry")
    ET.SubElement(geometry, geometry_kind, geometry_attrs)
    material = ET.SubElement(visual, "material", {"name": f"{name}_mat"})
    ET.SubElement(material, "color", {"rgba": rgba})


def _xyz(values: tuple[float, float, float]) -> str:
    return " ".join(f"{value:.12g}" for value in values)


def add_tcp_visual_marker(robot_description: str) -> str:
    """Attach a non-colliding visual marker to tool0 so GUI evidence shows TCP."""

    root = ET.fromstring(robot_description)
    if root.find(f"./link[@name='{TCP_VISUAL_LINK}']") is not None:
        return robot_description
    tool0 = root.find("./link[@name='tool0']")
    if tool0 is None:
        raise RuntimeError("generated URDF is missing tool0 link for TCP visual marker")

    _append_tcp_visuals(tool0, "tool0_direct")
    wrist_3 = root.find("./link[@name='wrist_3_link']")
    if wrist_3 is not None:
        # UR's wrist_3_link, flange, and tool0 share xyz=0 fixed origins; only orientation changes.
        _append_tcp_visuals(wrist_3, "wrist_3_tcp_position")
    link = ET.Element("link", {"name": TCP_VISUAL_LINK})
    _append_tcp_visuals(link, "tool0_child")

    joint = ET.Element("joint", {"name": TCP_VISUAL_JOINT, "type": "fixed"})
    ET.SubElement(joint, "parent", {"link": "tool0"})
    ET.SubElement(joint, "child", {"link": TCP_VISUAL_LINK})
    ET.SubElement(joint, "origin", {"xyz": "0 0 0", "rpy": "0 0 0"})

    root.append(link)
    root.append(joint)
    return ET.tostring(root, encoding="unicode")


def _append_tcp_visuals(link: ET.Element, prefix: str) -> None:
    visual = ET.SubElement(link, "visual")
    ET.SubElement(visual, "origin", {"xyz": "0 0 0", "rpy": "0 0 0"})
    geometry = ET.SubElement(visual, "geometry")
    ET.SubElement(geometry, "sphere", {"radius": "0.025"})
    material = ET.SubElement(visual, "material", {"name": f"{prefix}_tcp_visual_magenta"})
    ET.SubElement(material, "color", {"rgba": "1.0 0.0 1.0 1.0"})

    visual = ET.SubElement(link, "visual", {"name": f"{prefix}_tcp_visual_upright_mast"})
    ET.SubElement(visual, "origin", {"xyz": "0 0 0.055", "rpy": "0 0 0"})
    geometry = ET.SubElement(visual, "geometry")
    ET.SubElement(geometry, "cylinder", {"radius": "0.006", "length": "0.110"})
    material = ET.SubElement(visual, "material", {"name": f"{prefix}_tcp_visual_upright_mast_mat"})
    ET.SubElement(material, "color", {"rgba": "1.0 0.0 1.0 1.0"})

    axis_specs = [
        ("tcp_visual_x_axis", "0.035 0 0", "0 1.57079632679 0", "1.0 0.0 0.0 1.0"),
        ("tcp_visual_y_axis", "0 0.035 0", "1.57079632679 0 0", "0.0 0.2 1.0 1.0"),
        ("tcp_visual_z_axis", "0 0 0.035", "0 0 0", "1.0 1.0 1.0 1.0"),
    ]
    for name, xyz, rpy, rgba in axis_specs:
        visual = ET.SubElement(link, "visual", {"name": f"{prefix}_{name}"})
        ET.SubElement(visual, "origin", {"xyz": xyz, "rpy": rpy})
        geometry = ET.SubElement(visual, "geometry")
        ET.SubElement(geometry, "cylinder", {"radius": "0.004", "length": "0.070"})
        material = ET.SubElement(visual, "material", {"name": f"{prefix}_{name}_mat"})
        ET.SubElement(material, "color", {"rgba": rgba})


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


def default_action_result_timeout_s(*, duration_s: float, entry_duration_s: float) -> float:
    nominal_s = max(0.0, float(duration_s)) + max(0.0, float(entry_duration_s))
    return max(
        ACTION_RESULT_TIMEOUT_MIN_S,
        nominal_s * ACTION_RESULT_TIMEOUT_SLOWDOWN_FACTOR + ACTION_RESULT_TIMEOUT_EXTRA_S,
    )


def execute_joint_trajectory(
    joint_points: list[list[float]],
    *,
    duration_s: float,
    action_name: str = ACTION_NAME,
    joint_state_topic: str = "/joint_states",
    entry_duration_s: float = 4.0,
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
        sample_deadline = time.monotonic() + 2.0
        while not node.samples and time.monotonic() < sample_deadline:
            rclpy.spin_once(node, timeout_sec=0.05)

        goal = FollowJointTrajectory.Goal()
        goal.trajectory.joint_names = JOINT_NAMES
        goal_points = list(joint_points)
        current_positions = node.samples[-1] if node.samples else None
        if current_positions is not None:
            goal_points.insert(0, current_positions)
        count = len(joint_points)
        for index, positions in enumerate(goal_points):
            point = JointTrajectoryPoint()
            point.positions = positions
            if current_positions is not None:
                if index == 0:
                    t_s = 0.0
                else:
                    t_s = entry_duration_s + duration_s * (index - 1) / max(count - 1, 1)
            else:
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
        timeout = (
            float(result_timeout_s)
            if result_timeout_s is not None
            else default_action_result_timeout_s(duration_s=duration_s, entry_duration_s=entry_duration_s)
        )
        result_wait_started_s = time.monotonic()
        rclpy.spin_until_future_complete(node, result_future, timeout_sec=timeout)
        result_wait_elapsed_s = time.monotonic() - result_wait_started_s
        if not result_future.done():
            return {
                "ok": False,
                "action_accepted": True,
                "result_status": None,
                "result_error_code": None,
                "result_timeout_s": timeout,
                "result_wait_elapsed_s": result_wait_elapsed_s,
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
            "entry_point_from_joint_states": current_positions is not None,
            "entry_duration_s": entry_duration_s if current_positions is not None else 0.0,
            "result_status": int(wrapped.status),
            "result_error_code": error_code,
            "result_error_string": str(wrapped.result.error_string),
            "result_timeout_s": timeout,
            "result_wait_elapsed_s": result_wait_elapsed_s,
            "observed_joint_state_samples": len(node.samples),
            "observed_motion": _observed_motion(node.samples),
            "blocker": None if error_code == FollowJointTrajectory.Result.SUCCESSFUL else "action_result_error",
            "action_name": action_name,
        }
    finally:
        node.destroy_node()
        rclpy.shutdown()


def execute_force_closed_loop(
    stage_id: str,
    references: list[ReferencePoint],
    output_dir: Path,
    *,
    action_name: str = ACTION_NAME,
    joint_state_topic: str = "/joint_states",
    server_timeout_s: float = 10.0,
    target_load_n: float = DEFAULT_TARGET_LOAD_N,
    contact_surface_z_m: float = CONTACT_SURFACE_Z_M,
    stiffness_n_m: float = DEFAULT_CONTACT_STIFFNESS_N_M,
    force_tolerance_n: float = 0.75,
) -> dict[str, Any]:
    import numpy as np
    import pinocchio as pin
    import rclpy
    from builtin_interfaces.msg import Duration
    from control_msgs.action import FollowJointTrajectory
    from rclpy.action import ActionClient
    from rclpy.node import Node
    from sensor_msgs.msg import JointState
    from trajectory_msgs.msg import JointTrajectoryPoint

    from .step5a_cartesian_cycloid_motion import build_calibrated_model, fk_tool0_base, solve_tool0_ik

    class ForceLoopNode(Node):
        def __init__(self) -> None:
            super().__init__("ur10e_gazebo_force_closed_loop_runner")
            self.samples: list[list[float]] = []
            self.client = ActionClient(self, FollowJointTrajectory, action_name)
            self.create_subscription(JointState, joint_state_topic, self._on_joint_state, 10)

        def _on_joint_state(self, msg: JointState) -> None:
            by_name = dict(zip(msg.name, msg.position))
            if all(name in by_name for name in JOINT_NAMES):
                self.samples.append([float(by_name[name]) for name in JOINT_NAMES])

        def wait_for_joint_sample(self, timeout_s: float) -> list[float] | None:
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                rclpy.spin_once(self, timeout_sec=0.05)
                if self.samples:
                    return self.samples[-1]
            return None

        def send_short_goal(self, start_q: list[float], target_q: list[float], duration_s: float) -> dict[str, Any]:
            goal = FollowJointTrajectory.Goal()
            goal.trajectory.joint_names = JOINT_NAMES
            for t_s, positions in ((0.0, start_q), (max(duration_s, 0.05), target_q)):
                point = JointTrajectoryPoint()
                point.positions = positions
                point.time_from_start = Duration(sec=int(t_s), nanosec=int((t_s % 1.0) * 1_000_000_000))
                goal.trajectory.points.append(point)

            send_future = self.client.send_goal_async(goal)
            rclpy.spin_until_future_complete(self, send_future, timeout_sec=server_timeout_s)
            handle = send_future.result()
            if handle is None or not handle.accepted:
                return {"ok": False, "accepted": bool(handle and handle.accepted), "error_code": None, "blocker": "action_goal_rejected"}
            result_future = handle.get_result_async()
            rclpy.spin_until_future_complete(self, result_future, timeout_sec=max(duration_s + 5.0, 10.0))
            if not result_future.done():
                return {"ok": False, "accepted": True, "error_code": None, "blocker": "action_result_timeout"}
            wrapped = result_future.result()
            error_code = int(wrapped.result.error_code)
            return {
                "ok": error_code == FollowJointTrajectory.Result.SUCCESSFUL,
                "accepted": True,
                "error_code": error_code,
                "error_string": str(wrapped.result.error_string),
                "status": int(wrapped.status),
                "blocker": None if error_code == FollowJointTrajectory.Result.SUCCESSFUL else "action_result_error",
            }

    if not references:
        raise RuntimeError("force closed-loop execution has no reference points")
    output_dir.mkdir(parents=True, exist_ok=True)
    trace_path = output_dir / "force_closed_loop_trace.csv"
    model_bundle = build_calibrated_model()

    rclpy.init()
    node = ForceLoopNode()
    rows: list[dict[str, Any]] = []
    joint_samples_before = 0
    goal_failures: list[dict[str, Any]] = []
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
                    "force_closed_loop": False,
                    "force_loop_trace_path": str(trace_path),
                }

        current_q = node.wait_for_joint_sample(timeout_s=2.0)
        if current_q is None:
            return {
                "ok": False,
                "action_accepted": False,
                "result_status": None,
                "result_error_code": None,
                "observed_joint_state_samples": len(node.samples),
                "observed_motion": False,
                "blocker": "joint_state_unavailable",
                "action_name": action_name,
                "force_closed_loop": False,
                "force_loop_trace_path": str(trace_path),
            }
        joint_samples_before = len(node.samples)
        anchor_rotation = fk_tool0_base(model_bundle, np.array(current_q, dtype=float)).rotation.copy()
        equilibrium_z_m = contact_surface_z_m - target_load_n / stiffness_n_m
        normal_gain_m_per_n = 0.00025
        max_approach_step_m = 0.006
        max_retract_step_m = 0.003
        max_abs_force_error_n = 0.0
        settled_errors: list[float] = []
        contact_sample_count = 0
        accepted_count = 0
        prev_z_m: float | None = None
        prev_t_s: float | None = None
        initial_samples = list(node.samples)

        for index, ref in enumerate(references):
            current_q = node.wait_for_joint_sample(timeout_s=1.0) or current_q
            fk = fk_tool0_base(model_bundle, np.array(current_q, dtype=float))
            measured_z_m = float(fk.translation[2])
            if prev_z_m is None or prev_t_s is None:
                normal_velocity_m_s = 0.0
            else:
                dt_prev = max(ref.t_s - prev_t_s, 1e-6)
                normal_velocity_m_s = (measured_z_m - prev_z_m) / dt_prev
            penetration_m = max(0.0, contact_surface_z_m - measured_z_m)
            normal_load_n = stiffness_n_m * penetration_m
            force_error_n = target_load_n - normal_load_n
            max_abs_force_error_n = max(max_abs_force_error_n, abs(force_error_n))
            if normal_load_n > 0.25:
                contact_sample_count += 1

            if normal_load_n < 0.25:
                next_z_m = max(equilibrium_z_m, measured_z_m - max_approach_step_m)
            else:
                z_delta_m = -normal_gain_m_per_n * force_error_n
                z_delta_m = max(-max_approach_step_m, min(max_retract_step_m, z_delta_m))
                next_z_m = measured_z_m + z_delta_m
            next_z_m = max(contact_surface_z_m - 0.006, min(0.240, next_z_m))

            target = pin.SE3(anchor_rotation, np.array([ref.x_m, ref.y_m, next_z_m], dtype=float))
            solved_q, ik_error_m = solve_tool0_ik(
                model_bundle,
                np.array(current_q, dtype=float),
                target,
                damping=1e-4,
                max_iters=300,
                tolerance_m=1e-5,
            )
            target_q = [float(value) for value in solved_q.tolist()]
            next_t_s = references[index + 1].t_s if index + 1 < len(references) else ref.t_s + 0.2
            goal_duration_s = max(0.05, min(0.75, next_t_s - ref.t_s))
            goal_result = node.send_short_goal(current_q, target_q, goal_duration_s)
            accepted_count += 1 if goal_result.get("accepted") else 0
            if not goal_result.get("ok"):
                goal_failures.append({"index": index, "t_s": ref.t_s, **goal_result})
                break

            if contact_sample_count > 3:
                settled_errors.append(abs(force_error_n))
            rows.append(
                {
                    "stage_id": stage_id,
                    "index": index,
                    "t_s": ref.t_s,
                    "segment": ref.segment,
                    "reference_x_m": ref.x_m,
                    "reference_y_m": ref.y_m,
                    "measured_z_m": measured_z_m,
                    "commanded_z_m": next_z_m,
                    "surface_z_m": contact_surface_z_m,
                    "penetration_m": penetration_m,
                    "normal_velocity_m_s": normal_velocity_m_s,
                    "normal_load_n": normal_load_n,
                    "target_load_n": target_load_n,
                    "force_error_n": force_error_n,
                    "ik_error_m": float(ik_error_m),
                    "goal_duration_s": goal_duration_s,
                    "goal_error_code": goal_result.get("error_code"),
                    "goal_ok": bool(goal_result.get("ok")),
                }
            )
            current_q = target_q
            prev_z_m = measured_z_m
            prev_t_s = ref.t_s

        fieldnames = [
            "stage_id",
            "index",
            "t_s",
            "segment",
            "reference_x_m",
            "reference_y_m",
            "measured_z_m",
            "commanded_z_m",
            "surface_z_m",
            "penetration_m",
            "normal_velocity_m_s",
            "normal_load_n",
            "target_load_n",
            "force_error_n",
            "ik_error_m",
            "goal_duration_s",
            "goal_error_code",
            "goal_ok",
        ]
        with trace_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        settled_within = [err <= force_tolerance_n for err in settled_errors]
        settled_fraction = (
            sum(1 for value in settled_within if value) / len(settled_within)
            if settled_within
            else 0.0
        )
        mean_abs_settled_error_n = (
            sum(settled_errors) / len(settled_errors)
            if settled_errors
            else None
        )
        ok = (
            not goal_failures
            and trace_path.is_file()
            and contact_sample_count >= 5
            and bool(settled_errors)
            and settled_fraction >= 0.85
        )
        first_error = goal_failures[0] if goal_failures else None
        return {
            "ok": ok,
            "action_accepted": accepted_count > 0 and not goal_failures,
            "entry_point_from_joint_states": True,
            "entry_duration_s": 0.0,
            "result_status": 4 if ok else (first_error or {}).get("status"),
            "result_error_code": 0 if ok else (first_error or {}).get("error_code"),
            "result_error_string": "Gazebo force closed-loop completed" if ok else str((first_error or {}).get("blocker")),
            "observed_joint_state_samples": len(node.samples),
            "observed_motion": _observed_motion(initial_samples + node.samples),
            "blocker": None if ok else ((first_error or {}).get("blocker") or "force_closed_loop_acceptance_failed"),
            "action_name": action_name,
            "force_closed_loop": ok,
            "force_loop_trace_path": str(trace_path),
            "force_loop": {
                "schema": "ur10e_gazebo_force_closed_loop_v1",
                "force_source": "gazebo_joint_state_fk_virtual_contact_model",
                "runtime_feedback": "joint_states_to_fk_to_normal_load_to_next_joint_goal",
                "contact_surface_z_m": contact_surface_z_m,
                "target_load_n": target_load_n,
                "stiffness_n_m": stiffness_n_m,
                "reaction_normal": [0.0, 0.0, 1.0],
                "approach_normal": [0.0, 0.0, -1.0],
                "normal_load_definition": "dot(force_base, reaction_normal)",
                "sample_count": len(rows),
                "contact_sample_count": contact_sample_count,
                "accepted_goal_count": accepted_count,
                "goal_failure_count": len(goal_failures),
                "max_abs_force_error_n": max_abs_force_error_n,
                "mean_abs_settled_force_error_n": mean_abs_settled_error_n,
                "settled_within_tolerance_fraction": settled_fraction,
                "force_tolerance_n": force_tolerance_n,
                "trace_path": str(trace_path),
            },
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
        if stage_id in CONTACT_STAGE_IDS:
            execution = execute_force_closed_loop(stage_id, build_reference_rows(stage_id), stage_dir)
        else:
            execution = execute_joint_trajectory(joint_points, duration_s=float(metrics["duration_s"]))
    artifact = offline.build_stage_artifact(stage_id)
    force_closed_loop = bool(execution and execution.get("force_closed_loop"))
    force_loop = execution.get("force_loop") if execution else None
    summary = {
        "schema": "ur10e_gazebo_matrix_stage_result_v1",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "stage_id": stage_id,
        "mode": "gazebo_ros2_control",
        "gazebo_only": True,
        "live_robot_command_authorized": False,
        "force_physics_closed_loop": force_closed_loop,
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
        "force_loop": force_loop,
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
            "force_closed_loop": force_closed_loop if stage_id in CONTACT_STAGE_IDS and execute else None,
            "force_loop_trace_written": bool(force_loop and Path(str(force_loop.get("trace_path"))).is_file())
            if force_loop
            else None,
            "settled_force_within_tolerance_fraction": force_loop.get("settled_within_tolerance_fraction")
            if force_loop
            else None,
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
    stage_ids = ["step7", "step8"] if stage == "step7_8" else list(offline.STAGE_REGISTRY) if stage == "all" else [stage]
    if stage not in {"all", "step7_8"} and stage not in offline.STAGE_REGISTRY:
        raise SystemExit(f"unknown stage {stage!r}; expected all, step7_8, or one of {', '.join(offline.STAGE_REGISTRY)}")
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
    parser.add_argument("--stage", default="all", choices=["all", "step7_8", *offline.STAGE_REGISTRY.keys()])
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
