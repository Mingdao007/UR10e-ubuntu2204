from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
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
CONTACT_SURFACE_REAL_MESH_VISUAL_NAME = "real_surface_mesh_visual"
CONTACT_SURFACE_REAL_MESH_URI = (
    "package://ur10e_example_controllers/meshes/contact_surface/"
    "two_piece_surface_smooth_v11_3mm_thick.stl"
)
CONTACT_SURFACE_REAL_MESH_SOURCE_ASSET = (
    "/home/andy/Documents/xwechat_files/wxid_yn52rrzphgdv21_b48e/msg/file/2026-06/"
    "two_piece_surface_smooth_v11_3mm_thick.stl"
)
CONTACT_SURFACE_COUPON_MESH_SOURCE_ASSET = (
    "/home/andy/Documents/xwechat_files/wxid_yn52rrzphgdv21_b48e/msg/file/2026-06/"
    "coupon_v11_smooth_seam_30x30_3mm.stl"
)
CONTACT_SURFACE_REAL_MESH_SCALE = (0.001, 0.001, 0.001)
CONTACT_SURFACE_REAL_MESH_VISUAL_POSE = (
    0.0364678879,
    0.075,
    -0.0968663777,
    math.pi / 2.0,
    0.0,
    0.0,
)
CONTACT_SURFACE_REAL_MESH_RAW_BBOX_MM = {
    "min": [-111.46788787841797, 87.33544921875, 0.0],
    "max": [38.532108306884766, 104.91121673583984, 150.0],
    "size": [150.0, 17.575767517089844, 150.0],
}
CONTACT_SURFACE_REAL_MESH_ORIENTED_BBOX_M = {
    "size": [0.15000000596046448, 0.15000000596046448, 0.017575768753886223],
    "top_z_m": CONTACT_SURFACE_Z_M,
    "bottom_z_m": CONTACT_SURFACE_Z_M - 0.017575768753886223,
}
DEFAULT_TARGET_LOAD_N = 5.0
DEFAULT_CONTACT_STIFFNESS_N_M = 2500.0
TCP_VISUAL_LINK = "tool0_tcp_visual_marker"
TCP_VISUAL_JOINT = "tool0_tcp_visual_marker_joint"
EOAT_VISUAL_LINK = "real_aligned_eoat_visual_stack"
EOAT_VISUAL_JOINT = "real_aligned_eoat_visual_stack_joint"
EOAT_REAL_MESH_VISUAL_NAME = "eoat_real_ksm8n_v13_assembly_mesh_visual"
EOAT_REAL_MESH_URI = (
    "package://ur10e_example_controllers/meshes/eoat/"
    "ur5e_ksm8n_ball_transfer_tool_v13_assembly.stl"
)
EOAT_REAL_MESH_SOURCE_ASSET = (
    "/home/andy/ur10e_lab_vault/onrobot/hex_e_v2_3010007655/eoat_design/"
    "v13_ksm8n_receiver_5p3mm_side_window_85mm/"
    "ur5e_ksm8n_ball_transfer_tool_v13_assembly.stl"
)
EOAT_REAL_MESH_SCALE = (0.001, 0.001, 0.001)
EOAT_REAL_MESH_RAW_BBOX_MM = {
    "min": [-35.99661, -35.99915, 0.0],
    "max": [36.0, 35.99915, 73.9],
    "size": [71.99661, 71.9983, 73.9],
}
EOAT_REAL_MESH_SCALED_BBOX_M = {
    "size": [0.07199661, 0.0719983, 0.0739],
    "center": [0.000001695, 0.0, 0.03695],
}
ACTIVE_TCP_FRAME = "base_to_active_tcp"
ACTIVE_TCP_LINK = "active_tcp"
ACTIVE_TCP_JOINT = "active_tcp_joint"
TOOL0_FRAME = "base_to_tool0"
GAZEBO_WORLD_FRAME = "gazebo_world"
BASE_TO_GAZEBO_WORLD_RPY = (0.0, 0.0, math.pi)
ACTIVE_TCP_OFFSET_TOOL0_M = (
    0.0000018186503701174852,
    0.00000022293003722353485,
    0.12209917288991741,
)
EOAT_REQUIRED_VISUAL_NAMES = frozenset(
    {
        EOAT_REAL_MESH_VISUAL_NAME,
    }
)
EOAT_VIEWER_AFFORDANCE_VISUAL_NAMES = frozenset()
TOOL0_EOAT_VIEWER_VISUAL_NAMES = frozenset()
EOAT_REQUIRED_COLLISION_NAMES = frozenset(
    {
        "eoat_flange_adapter_collision",
        "eoat_kunwei_sensor_body_collision",
        "eoat_left_bracket_collision",
        "eoat_right_bracket_collision",
        "eoat_tool_plate_collision",
        "eoat_contact_probe_collision",
        "eoat_contact_pad_collision",
    }
)
EOAT_CONTACT_COLLISION_NAMES = frozenset(
    {
        "eoat_contact_probe_collision",
        "eoat_contact_pad_collision",
    }
)
FORCE_CONTACT_SOURCE = "gazebo_joint_state_fk_virtual_surface_model"
FORCE_CONTACT_PHYSICS_PROVEN = False
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
    return add_real_aligned_eoat_visual_stack(robot_description)


def add_real_aligned_eoat_visual_stack(robot_description: str) -> str:
    """Attach the local STL EOAT mesh as the primary visual, with simplified collisions."""

    root = ET.fromstring(robot_description)
    tool0 = root.find("./link[@name='tool0']")
    if tool0 is None:
        raise RuntimeError("generated URDF is missing tool0 link for EOAT visual stack")
    if root.find(f"./link[@name='{EOAT_VISUAL_LINK}']") is not None:
        _append_active_tcp_frame(root)
        return ET.tostring(root, encoding="unicode")

    link = ET.Element("link", {"name": EOAT_VISUAL_LINK})
    _append_visual_proxy_inertial(link)
    _append_eoat_mesh_visual(
        link,
        name=EOAT_REAL_MESH_VISUAL_NAME,
        filename=EOAT_REAL_MESH_URI,
        scale=EOAT_REAL_MESH_SCALE,
        rgba="0.54 0.54 0.50 1.0",
    )
    _append_eoat_collision(
        link,
        name="eoat_flange_adapter_collision",
        xyz=(0.0, 0.0, 0.012),
        rpy="0 0 0",
        geometry_kind="cylinder",
        geometry_attrs={"radius": "0.058", "length": "0.024"},
    )
    _append_eoat_collision(
        link,
        name="eoat_kunwei_sensor_body_collision",
        xyz=(0.0, 0.0, 0.052),
        rpy="0 0 0",
        geometry_kind="cylinder",
        geometry_attrs={"radius": "0.043", "length": "0.060"},
    )
    _append_eoat_collision(
        link,
        name="eoat_left_bracket_collision",
        xyz=(0.0, 0.046, 0.086),
        rpy="0 0 0",
        geometry_kind="box",
        geometry_attrs={"size": "0.100 0.014 0.040"},
    )
    _append_eoat_collision(
        link,
        name="eoat_right_bracket_collision",
        xyz=(0.0, -0.046, 0.086),
        rpy="0 0 0",
        geometry_kind="box",
        geometry_attrs={"size": "0.100 0.014 0.040"},
    )
    _append_eoat_collision(
        link,
        name="eoat_tool_plate_collision",
        xyz=(0.0, 0.0, 0.092),
        rpy="0 0 0",
        geometry_kind="box",
        geometry_attrs={"size": "0.118 0.074 0.016"},
    )
    _append_eoat_collision(
        link,
        name="eoat_contact_probe_collision",
        xyz=(0.0, 0.0, 0.106),
        rpy="0 0 0",
        geometry_kind="cylinder",
        geometry_attrs={"radius": "0.014", "length": "0.052"},
    )
    _append_eoat_collision(
        link,
        name="eoat_contact_pad_collision",
        xyz=(0.0, 0.0, 0.122),
        rpy="0 0 0",
        geometry_kind="box",
        geometry_attrs={"size": "0.052 0.052 0.010"},
    )

    joint = ET.Element("joint", {"name": EOAT_VISUAL_JOINT, "type": "fixed"})
    ET.SubElement(joint, "parent", {"link": "tool0"})
    ET.SubElement(joint, "child", {"link": EOAT_VISUAL_LINK})
    ET.SubElement(joint, "origin", {"xyz": "0 0 0", "rpy": "0 0 0"})

    root.append(link)
    root.append(joint)
    _append_active_tcp_frame(root)
    return ET.tostring(root, encoding="unicode")


def _append_active_tcp_frame(root: ET.Element) -> None:
    if root.find(f"./link[@name='{ACTIVE_TCP_LINK}']") is not None:
        return
    tcp_link = ET.Element("link", {"name": ACTIVE_TCP_LINK})
    inertial = ET.SubElement(tcp_link, "inertial")
    ET.SubElement(inertial, "origin", {"xyz": "0 0 0", "rpy": "0 0 0"})
    ET.SubElement(inertial, "mass", {"value": "1e-6"})
    ET.SubElement(
        inertial,
        "inertia",
        {
            "ixx": "1e-12",
            "ixy": "0",
            "ixz": "0",
            "iyy": "1e-12",
            "iyz": "0",
            "izz": "1e-12",
        },
    )
    tcp_joint = ET.Element("joint", {"name": ACTIVE_TCP_JOINT, "type": "fixed"})
    ET.SubElement(tcp_joint, "parent", {"link": "tool0"})
    ET.SubElement(tcp_joint, "child", {"link": ACTIVE_TCP_LINK})
    ET.SubElement(
        tcp_joint,
        "origin",
        {"xyz": " ".join(f"{value:.17g}" for value in ACTIVE_TCP_OFFSET_TOOL0_M), "rpy": "0 0 0"},
    )
    tcp_gazebo = ET.Element("gazebo", {"reference": ACTIVE_TCP_JOINT})
    ET.SubElement(tcp_gazebo, "preserveFixedJoint").text = "true"
    root.append(tcp_link)
    root.append(tcp_joint)
    root.append(tcp_gazebo)


def _append_visual_proxy_inertial(link: ET.Element) -> None:
    inertial = ET.SubElement(link, "inertial")
    ET.SubElement(inertial, "origin", {"xyz": "0 0 0.061", "rpy": "0 0 0"})
    ET.SubElement(inertial, "mass", {"value": "0.05"})
    ET.SubElement(
        inertial,
        "inertia",
        {
            "ixx": "1e-05",
            "ixy": "0",
            "ixz": "0",
            "iyy": "1e-05",
            "iyz": "0",
            "izz": "1e-05",
        },
    )


def _append_eoat_mesh_visual(
    link: ET.Element,
    *,
    name: str,
    filename: str,
    scale: tuple[float, float, float],
    rgba: str,
) -> None:
    visual = ET.SubElement(link, "visual", {"name": name})
    ET.SubElement(visual, "origin", {"xyz": "0 0 0", "rpy": "0 0 0"})
    geometry = ET.SubElement(visual, "geometry")
    ET.SubElement(
        geometry,
        "mesh",
        {
            "filename": filename,
            "scale": _xyz(scale),
        },
    )
    material = ET.SubElement(visual, "material", {"name": f"{name}_mat"})
    ET.SubElement(material, "color", {"rgba": rgba})


def _append_eoat_collision(
    link: ET.Element,
    *,
    name: str,
    xyz: tuple[float, float, float],
    rpy: str,
    geometry_kind: str,
    geometry_attrs: dict[str, str],
) -> None:
    collision = ET.SubElement(link, "collision", {"name": name})
    ET.SubElement(collision, "origin", {"xyz": _xyz(xyz), "rpy": rpy})
    geometry = ET.SubElement(collision, "geometry")
    ET.SubElement(geometry, geometry_kind, geometry_attrs)


def _xyz(values: tuple[float, float, float]) -> str:
    return " ".join(f"{value:.12g}" for value in values)


def active_tcp_xyz_from_tool0_pose(tool0_pose: Any) -> Any:
    import numpy as np

    return tool0_pose.translation + tool0_pose.rotation @ np.array(ACTIVE_TCP_OFFSET_TOOL0_M, dtype=float)


def tool0_pose_for_active_tcp_target(rotation: Any, active_tcp_xyz_m: Any) -> Any:
    import numpy as np
    import pinocchio as pin

    active_tcp_xyz = np.array(active_tcp_xyz_m, dtype=float)
    offset_base = rotation @ np.array(ACTIVE_TCP_OFFSET_TOOL0_M, dtype=float)
    return pin.SE3(rotation, active_tcp_xyz - offset_base)


def gazebo_world_xyz_from_base_xyz(base_xyz_m: Any) -> tuple[float, float, float]:
    values = [float(value) for value in base_xyz_m]
    if len(values) != 3:
        raise ValueError(f"expected xyz vector with 3 values, got {len(values)}")
    return (-values[0], -values[1], values[2])


def build_model_composition_audit(robot_description: str | None = None) -> dict[str, Any]:
    text = robot_description if robot_description is not None else generate_sim_robot_description()
    root = ET.fromstring(text)
    link_names = ["base_link", "wrist_3_link", "flange", "tool0", EOAT_VISUAL_LINK, TCP_VISUAL_LINK]
    links = []
    for name in link_names:
        link = root.find(f"./link[@name='{name}']")
        if link is None:
            links.append({"name": name, "present": False})
            continue
        visual_names = [visual.attrib.get("name", "") for visual in link.findall("visual")]
        collision_names = [collision.attrib.get("name", "") for collision in link.findall("collision")]
        links.append(
            {
                "name": name,
                "present": True,
                "visual_count": len(link.findall("visual")),
                "collision_count": len(link.findall("collision")),
                "visual_names": visual_names,
                "collision_names": collision_names,
            }
        )
    eoat = root.find(f"./link[@name='{EOAT_VISUAL_LINK}']")
    eoat_visual_names = {visual.attrib.get("name", "") for visual in eoat.findall("visual")} if eoat is not None else set()
    eoat_mesh_visuals = {}
    eoat_primitive_visual_names = []
    if eoat is not None:
        for visual in eoat.findall("visual"):
            name = visual.attrib.get("name", "")
            mesh = visual.find("./geometry/mesh")
            if mesh is not None:
                eoat_mesh_visuals[name] = {
                    "filename": mesh.attrib.get("filename"),
                    "scale": mesh.attrib.get("scale"),
                }
            else:
                eoat_primitive_visual_names.append(name)
    eoat_collision_names = (
        {collision.attrib.get("name", "") for collision in eoat.findall("collision")} if eoat is not None else set()
    )
    tool0 = root.find("./link[@name='tool0']")
    tool0_visual_names = {visual.attrib.get("name", "") for visual in tool0.findall("visual")} if tool0 is not None else set()
    joint = root.find(f"./joint[@name='{EOAT_VISUAL_JOINT}']")
    tcp_visual_link_present = root.find(f"./link[@name='{TCP_VISUAL_LINK}']") is not None
    return {
        "schema": "ur10e_gazebo_model_composition_audit_v2",
        "eoat_visual_link": EOAT_VISUAL_LINK,
        "eoat_visual_joint": EOAT_VISUAL_JOINT,
        "eoat_joint_parent": joint.find("parent").attrib.get("link") if joint is not None and joint.find("parent") is not None else None,
        "eoat_joint_child": joint.find("child").attrib.get("link") if joint is not None and joint.find("child") is not None else None,
        "eoat_joint_origin": joint.find("origin").attrib if joint is not None and joint.find("origin") is not None else None,
        "required_eoat_visuals": sorted(EOAT_REQUIRED_VISUAL_NAMES),
        "viewer_affordance_eoat_visuals": sorted(EOAT_VIEWER_AFFORDANCE_VISUAL_NAMES),
        "required_tool0_viewer_affordance_visuals": sorted(TOOL0_EOAT_VIEWER_VISUAL_NAMES),
        "present_tool0_viewer_affordance_visuals": sorted(TOOL0_EOAT_VIEWER_VISUAL_NAMES & tool0_visual_names),
        "missing_tool0_viewer_affordance_visuals": sorted(TOOL0_EOAT_VIEWER_VISUAL_NAMES - tool0_visual_names),
        "present_eoat_visuals": sorted(eoat_visual_names),
        "missing_eoat_visuals": sorted(EOAT_REQUIRED_VISUAL_NAMES - eoat_visual_names),
        "eoat_visual_count": len(eoat_visual_names),
        "eoat_primary_visual_mesh_name": EOAT_REAL_MESH_VISUAL_NAME,
        "eoat_primary_visual_mesh_uri": EOAT_REAL_MESH_URI,
        "eoat_primary_visual_mesh_source_asset": EOAT_REAL_MESH_SOURCE_ASSET,
        "eoat_primary_visual_mesh_scale": list(EOAT_REAL_MESH_SCALE),
        "eoat_primary_visual_mesh_raw_bbox_mm": EOAT_REAL_MESH_RAW_BBOX_MM,
        "eoat_primary_visual_mesh_scaled_bbox_m": EOAT_REAL_MESH_SCALED_BBOX_M,
        "eoat_mesh_visuals": eoat_mesh_visuals,
        "actual_eoat_mesh_visual_present": (
            eoat_mesh_visuals.get(EOAT_REAL_MESH_VISUAL_NAME, {}).get("filename") == EOAT_REAL_MESH_URI
            and eoat_mesh_visuals.get(EOAT_REAL_MESH_VISUAL_NAME, {}).get("scale") == _xyz(EOAT_REAL_MESH_SCALE)
        ),
        "eoat_primitive_visual_remnants": sorted(eoat_primitive_visual_names),
        "required_eoat_collisions": sorted(EOAT_REQUIRED_COLLISION_NAMES),
        "present_eoat_collisions": sorted(eoat_collision_names),
        "missing_eoat_collisions": sorted(EOAT_REQUIRED_COLLISION_NAMES - eoat_collision_names),
        "present_eoat_contact_collisions": sorted(EOAT_CONTACT_COLLISION_NAMES & eoat_collision_names),
        "eoat_collision_count": len(eoat.findall("collision")) if eoat is not None else None,
        "eoat_inertial_present": eoat.find("inertial") is not None if eoat is not None else None,
        "eoat_visual_proxy_policy": "actual_local_stl_primary_visual_with_simplified_collision_primitives",
        "eoat_collision_policy": (
            "eoat_collision_bodies_instantiated_for_inventory_only;"
            "contact_pair_log_missing;wrench_contact_correlation_missing;"
            "not_physical_gazebo_contact_physics_proven"
        ),
        "tcp_visual_link_present": tcp_visual_link_present,
        "redundant_tcp_marker_policy": "removed_from_generated_robot_description; active TCP marker is part of EOAT visual stack and optional external marker follows base_T_active_tcp",
        "active_tcp_offset_tool0_m": list(ACTIVE_TCP_OFFSET_TOOL0_M),
        "gazebo_world_frame": GAZEBO_WORLD_FRAME,
        "base_to_gazebo_world_rpy": list(BASE_TO_GAZEBO_WORLD_RPY),
        "force_contact_source": FORCE_CONTACT_SOURCE,
        "force_contact_physics_proven": FORCE_CONTACT_PHYSICS_PROVEN,
        "links": links,
        "plugin_count": len(root.findall(".//plugin")),
    }


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
        active_target_xyz = np.array([ref.x_m, ref.y_m, ref.z_m], dtype=float)
        target = tool0_pose_for_active_tcp_target(anchor_rotation, active_target_xyz)
        q, ik_error_m = solve_tool0_ik(
            model_bundle,
            q,
            target,
            damping=1e-4,
            max_iters=300,
            tolerance_m=1e-5,
        )
        fk = fk_tool0_base(model_bundle, q)
        tool0_xyz = [float(value) for value in fk.translation]
        active_tcp_xyz = active_tcp_xyz_from_tool0_pose(fk)
        active_tcp_values = [float(value) for value in active_tcp_xyz]
        cartesian_error_m = float(np.linalg.norm(active_tcp_xyz - active_target_xyz))
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
            "reference_frame": ACTIVE_TCP_FRAME,
            "reference_speed_m_s": ref.reference_speed_m_s,
            "commanded_fk_frame": ACTIVE_TCP_FRAME,
            "commanded_fk_x_m": active_tcp_values[0],
            "commanded_fk_y_m": active_tcp_values[1],
            "commanded_fk_z_m": active_tcp_values[2],
            "commanded_active_tcp_x_m": active_tcp_values[0],
            "commanded_active_tcp_y_m": active_tcp_values[1],
            "commanded_active_tcp_z_m": active_tcp_values[2],
            "commanded_tool0_x_m": tool0_xyz[0],
            "commanded_tool0_y_m": tool0_xyz[1],
            "commanded_tool0_z_m": tool0_xyz[2],
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
        "reference_frame",
        "reference_speed_m_s",
        "commanded_fk_frame",
        "commanded_fk_x_m",
        "commanded_fk_y_m",
        "commanded_fk_z_m",
        "commanded_active_tcp_x_m",
        "commanded_active_tcp_y_m",
        "commanded_active_tcp_z_m",
        "commanded_tool0_x_m",
        "commanded_tool0_y_m",
        "commanded_tool0_z_m",
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
        "reference_frame": ACTIVE_TCP_FRAME,
        "tool0_frame": TOOL0_FRAME,
        "active_tcp_offset_tool0_m": list(ACTIVE_TCP_OFFSET_TOOL0_M),
    }
    return trace_path, metrics, joint_points


def default_action_result_timeout_s(*, duration_s: float, entry_duration_s: float) -> float:
    nominal_s = max(0.0, float(duration_s)) + max(0.0, float(entry_duration_s))
    return max(
        ACTION_RESULT_TIMEOUT_MIN_S,
        nominal_s * ACTION_RESULT_TIMEOUT_SLOWDOWN_FACTOR + ACTION_RESULT_TIMEOUT_EXTRA_S,
    )


def build_action_timing_evidence(
    *,
    planned_trajectory_duration_s: float,
    entry_duration_s: float,
    result_wait_elapsed_s: float | None,
    observed_joint_state_samples: int,
    action_success: bool,
    timed_out: bool = False,
) -> dict[str, Any]:
    planned_s = max(0.0, float(planned_trajectory_duration_s))
    entry_s = max(0.0, float(entry_duration_s))
    commanded_goal_duration_s = planned_s + entry_s
    elapsed_s = None if result_wait_elapsed_s is None else max(0.0, float(result_wait_elapsed_s))
    ratio = None
    inferred_scale = None
    if elapsed_s is not None and commanded_goal_duration_s > 0.0:
        ratio = elapsed_s / commanded_goal_duration_s
        if elapsed_s > 0.0:
            inferred_scale = commanded_goal_duration_s / elapsed_s

    if elapsed_s is None:
        evidence_status = "missing_action_result_timing"
    elif timed_out:
        evidence_status = "action_result_timeout_timing_recorded"
    elif action_success:
        evidence_status = "action_result_timing_recorded"
    else:
        evidence_status = "action_result_error_timing_recorded"

    if ratio is None:
        root_cause_status = "root_cause_open_missing_wall_clock_action_timing"
    elif ratio > 1.05:
        root_cause_status = (
            "wall_clock_action_elapsed_slowdown_recorded_rtf_or_controller_speed_unresolved"
        )
    else:
        root_cause_status = (
            "wall_clock_action_elapsed_near_nominal_rtf_or_controller_speed_unresolved"
        )

    return {
        "schema": "ur10e_gazebo_action_timing_evidence_v2",
        "timing_evidence_source": "follow_joint_trajectory_result_elapsed_vs_goal_time_from_start",
        "elapsed_clock_domain": "wall_clock_monotonic",
        "commanded_duration_clock_domain": "trajectory_time_from_start",
        "duration_ratio_clock_domain": "wall_clock_monotonic_vs_commanded_trajectory_time_from_start",
        "planned_trajectory_duration_s": planned_s,
        "entry_duration_s": entry_s,
        "commanded_goal_duration_s": commanded_goal_duration_s,
        "action_result_elapsed_s": elapsed_s,
        "actual_vs_commanded_duration_ratio": ratio,
        "inferred_speed_scale_from_action_result": inferred_scale,
        "inferred_speed_scale_interpretation": (
            "Wall-clock elapsed ratio only; may reflect Gazebo real-time factor, "
            "controller speed scaling, action overhead, or settling."
        ),
        "sim_time_real_time_factor_confound": True,
        "rtf_or_controller_speed_unresolved": True,
        "observed_joint_state_samples": int(observed_joint_state_samples),
        "action_success": bool(action_success),
        "timed_out": bool(timed_out),
        "controller_speed_scaling_measured": False,
        "controller_speed_scaling_value": None,
        "controller_speed_scaling_source": None,
        "evidence_status": evidence_status,
        "timing_root_cause_status": root_cause_status,
        "claim_limit": (
            "Wall-clock action elapsed versus commanded trajectory duration is recorded; "
            "controller speed_scaling was not measured, and Gazebo real-time factor may explain the ratio."
        ),
    }


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
            self.sample_times_s: list[float] = []
            self.client = ActionClient(self, FollowJointTrajectory, action_name)
            self.create_subscription(JointState, joint_state_topic, self._on_joint_state, 10)

        def _on_joint_state(self, msg: JointState) -> None:
            by_name = dict(zip(msg.name, msg.position))
            if all(name in by_name for name in JOINT_NAMES):
                self.samples.append([float(by_name[name]) for name in JOINT_NAMES])
                self.sample_times_s.append(time.monotonic())

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
        applied_entry_duration_s = entry_duration_s if current_positions is not None else 0.0
        commanded_goal_duration_s = max(0.0, float(duration_s)) + max(0.0, float(applied_entry_duration_s))
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
            else default_action_result_timeout_s(duration_s=duration_s, entry_duration_s=applied_entry_duration_s)
        )
        result_wait_started_s = time.monotonic()
        rclpy.spin_until_future_complete(node, result_future, timeout_sec=timeout)
        result_wait_elapsed_s = time.monotonic() - result_wait_started_s
        joint_state_sample_span_s = (
            node.sample_times_s[-1] - node.sample_times_s[0] if len(node.sample_times_s) >= 2 else None
        )
        if not result_future.done():
            timing_evidence = build_action_timing_evidence(
                planned_trajectory_duration_s=duration_s,
                entry_duration_s=applied_entry_duration_s,
                result_wait_elapsed_s=result_wait_elapsed_s,
                observed_joint_state_samples=len(node.samples),
                action_success=False,
                timed_out=True,
            )
            return {
                "ok": False,
                "action_accepted": True,
                "entry_point_from_joint_states": current_positions is not None,
                "entry_duration_s": applied_entry_duration_s,
                "planned_trajectory_duration_s": duration_s,
                "commanded_goal_duration_s": commanded_goal_duration_s,
                "result_status": None,
                "result_error_code": None,
                "result_timeout_s": timeout,
                "result_wait_elapsed_s": result_wait_elapsed_s,
                "joint_state_sample_span_s": joint_state_sample_span_s,
                "observed_joint_state_samples": len(node.samples),
                "observed_motion": _observed_motion(node.samples),
                "timing_evidence": timing_evidence,
                "blocker": "action_result_timeout",
                "action_name": action_name,
            }
        wrapped = result_future.result()
        error_code = int(wrapped.result.error_code)
        action_success = error_code == FollowJointTrajectory.Result.SUCCESSFUL
        timing_evidence = build_action_timing_evidence(
            planned_trajectory_duration_s=duration_s,
            entry_duration_s=applied_entry_duration_s,
            result_wait_elapsed_s=result_wait_elapsed_s,
            observed_joint_state_samples=len(node.samples),
            action_success=action_success,
        )
        return {
            "ok": action_success,
            "action_accepted": True,
            "entry_point_from_joint_states": current_positions is not None,
            "entry_duration_s": applied_entry_duration_s,
            "planned_trajectory_duration_s": duration_s,
            "commanded_goal_duration_s": commanded_goal_duration_s,
            "result_status": int(wrapped.status),
            "result_error_code": error_code,
            "result_error_string": str(wrapped.result.error_string),
            "result_timeout_s": timeout,
            "result_wait_elapsed_s": result_wait_elapsed_s,
            "joint_state_sample_span_s": joint_state_sample_span_s,
            "observed_joint_state_samples": len(node.samples),
            "observed_motion": _observed_motion(node.samples),
            "timing_evidence": timing_evidence,
            "blocker": None if action_success else "action_result_error",
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
            active_tcp_xyz = active_tcp_xyz_from_tool0_pose(fk)
            measured_z_m = float(active_tcp_xyz[2])
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

            target_active_tcp_xyz = np.array([ref.x_m, ref.y_m, next_z_m], dtype=float)
            target = tool0_pose_for_active_tcp_target(anchor_rotation, target_active_tcp_xyz)
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
                    "active_tcp_x_m": float(active_tcp_xyz[0]),
                    "active_tcp_y_m": float(active_tcp_xyz[1]),
                    "active_tcp_z_m": measured_z_m,
                    "tool0_x_m": float(fk.translation[0]),
                    "tool0_y_m": float(fk.translation[1]),
                    "tool0_z_m": float(fk.translation[2]),
                    "measured_z_m": measured_z_m,
                    "commanded_active_tcp_z_m": next_z_m,
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
            "active_tcp_x_m",
            "active_tcp_y_m",
            "active_tcp_z_m",
            "tool0_x_m",
            "tool0_y_m",
            "tool0_z_m",
            "measured_z_m",
            "commanded_active_tcp_z_m",
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
            "force_contact_source": FORCE_CONTACT_SOURCE,
            "force_contact_physics_proven": FORCE_CONTACT_PHYSICS_PROVEN,
            "force_loop_trace_path": str(trace_path),
            "force_loop": {
                "schema": "ur10e_gazebo_force_closed_loop_v1",
                "force_source": FORCE_CONTACT_SOURCE,
                "runtime_feedback": "joint_states_to_fk_to_normal_load_to_next_joint_goal",
                "control_frame": ACTIVE_TCP_FRAME,
                "tool_frame": TOOL0_FRAME,
                "active_tcp_offset_tool0_m": list(ACTIVE_TCP_OFFSET_TOOL0_M),
                "contact_surface_z_m": contact_surface_z_m,
                "contact_physics_proven": FORCE_CONTACT_PHYSICS_PROVEN,
                "contact_physics_status": "software_virtual_surface_model_not_gazebo_collision_physics",
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
    software_force_loop_success = bool(execution and execution.get("force_closed_loop"))
    force_physics_closed_loop = bool(execution and execution.get("force_contact_physics_proven"))
    force_loop = execution.get("force_loop") if execution else None
    timing_evidence = execution.get("timing_evidence") if execution else None
    summary = {
        "schema": "ur10e_gazebo_matrix_stage_result_v1",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "stage_id": stage_id,
        "mode": "gazebo_ros2_control",
        "gazebo_only": True,
        "live_robot_command_authorized": False,
        "force_physics_closed_loop": force_physics_closed_loop,
        "software_force_loop_success": software_force_loop_success,
        "force_contact_source": execution.get("force_contact_source") if execution else None,
        "force_contact_physics_proven": force_physics_closed_loop,
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
        "timing_evidence": timing_evidence,
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
            "force_closed_loop": software_force_loop_success if stage_id in CONTACT_STAGE_IDS and execute else None,
            "force_contact_physics_proven": force_physics_closed_loop if stage_id in CONTACT_STAGE_IDS and execute else None,
            "actual_vs_commanded_timing_recorded": bool(
                timing_evidence and timing_evidence.get("actual_vs_commanded_duration_ratio") is not None
            )
            if execute and stage_id not in CONTACT_STAGE_IDS
            else None,
            "controller_speed_scaling_measured": bool(
                timing_evidence and timing_evidence.get("controller_speed_scaling_measured")
            )
            if execute and stage_id not in CONTACT_STAGE_IDS
            else None,
            "timing_root_cause_status": timing_evidence.get("timing_root_cause_status")
            if timing_evidence
            else None,
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


def observer_visual_criteria(row: dict[str, Any]) -> dict[str, bool]:
    marker_source = str(row.get("marker_pose_source") or row.get("pose_source") or "")
    pose_frame = str(row.get("pose_frame") or row.get("marker_pose_frame") or "")
    clean_capture = bool(row.get("clean_scene_capture") or row.get("obstructive_ui_panels_absent"))
    review_source = str(row.get("observer_visual_review_source") or "")
    observer_review_present = bool(row.get("observer_review_present")) and review_source not in {
        "",
        "pending_observer_review",
        "runtime_artifact_prerequisites_v1",
    }
    criteria = {
        "observer_review_present": observer_review_present,
        "gui_evidence_captured": bool(row.get("gui_evidence_captured")),
        "robot_arm_visible": bool(row.get("robot_posture_visible")),
        "eoat_tooling_visible": bool(row.get("eoat_tooling_visible")),
        "actual_eoat_mesh_visual_present": bool(row.get("actual_eoat_mesh_visual_present")),
        "actual_contact_surface_mesh_visual_present": bool(row.get("actual_contact_surface_mesh_visual_present")),
        "live_actual_eoat_mesh_visual_present": bool(row.get("live_scene_actual_eoat_mesh_visuals_present")),
        "live_actual_contact_surface_mesh_visual_present": bool(
            row.get("live_scene_actual_contact_surface_mesh_visuals_present")
        ),
        "marker_style_auxiliary_only": row.get("marker_style") == "minimal_tcp_dot",
        "primitive_proxy_not_primary_visual": bool(row.get("primitive_proxy_not_primary_visual")),
        "primitive_proxy_not_main_visual_cue": bool(row.get("primitive_proxy_not_main_visual_cue")),
        "observer_level_demo_realism": bool(row.get("observer_level_demo_realism")),
        "active_tcp_marker_visible": bool(row.get("tcp_marker_visible")),
        "surface_path_visible": bool(row.get("surface_path_visible")),
        "robot_tool_surface_relation_visible": bool(row.get("robot_tool_surface_relation_visible")),
        "active_tcp_pose_source_valid": marker_source == "joint_states_to_runner_fk_active_tcp_base_to_gazebo_world",
        "active_tcp_pose_frame_valid": pose_frame == GAZEBO_WORLD_FRAME,
        "clean_scene_capture": clean_capture,
    }
    if row.get("view") == "context_overview":
        # Context evidence may include UI if a clean interaction/detail pair is also recorded separately.
        criteria["clean_scene_capture"] = bool(row.get("clean_scene_capture") or row.get("obstructive_ui_panels_absent") or row.get("context_ui_allowed"))
    return criteria


def populate_observer_visual_pass(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    criteria = observer_visual_criteria(payload)
    failures = [name for name, ok in criteria.items() if not ok]
    payload["observer_visual_criteria"] = criteria
    payload["observer_visual_pass"] = not failures
    payload["observer_visual_failure_reasons"] = failures
    payload["observer_visual_gate_version"] = "observer_visual_gate_v4_live_mesh_foundation"
    return payload


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
    model_composition_audit = build_model_composition_audit()
    model_audit_path = output_dir / "model_composition_audit.json"
    model_audit_path.write_text(json.dumps(model_composition_audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
        "model_composition_audit_path": str(model_audit_path),
        "model_composition_audit": model_composition_audit,
        "stages": [json.loads(path.read_text(encoding="utf-8")) for path in summaries],
        "git_provenance": build_git_provenance(WORKSPACE),
    }
    path = output_dir / "matrix_summary.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def build_git_provenance(repo: Path = WORKSPACE) -> dict[str, Any]:
    """Best-effort source provenance for generated Gazebo run artifacts."""

    def git(args: list[str]) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(repo), *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=5.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    branch = git(["rev-parse", "--abbrev-ref", "HEAD"])
    commit = git(["rev-parse", "HEAD"])
    upstream = git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    dirty_entries = git(["status", "--short"])
    ahead = behind = None
    if upstream:
        counts = git(["rev-list", "--left-right", "--count", f"{upstream}...HEAD"])
        if counts:
            parts = counts.split()
            if len(parts) == 2:
                behind, ahead = int(parts[0]), int(parts[1])
    return {
        "repo": str(repo),
        "branch": branch,
        "commit": commit,
        "upstream": upstream,
        "dirty": bool(dirty_entries),
        "dirty_entries": dirty_entries.splitlines() if dirty_entries else [],
        "ahead": ahead,
        "behind": behind,
    }


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
