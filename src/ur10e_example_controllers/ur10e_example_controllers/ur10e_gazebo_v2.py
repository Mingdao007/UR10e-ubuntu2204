"""Gazebo Fortress v2 lane contracts and robot-description hardening.

This module is intentionally free of ROS imports so its fail-closed contracts
can be unit-tested on the Mac.  Launch/runtime code supplies the calibrated UR
description, then this module makes the selected command interface exclusive
and attaches native Gazebo contact and force/torque sensors to the robot.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


JOINT_NAMES = (
    "shoulder_pan_joint",
    "shoulder_lift_joint",
    "elbow_joint",
    "wrist_1_joint",
    "wrist_2_joint",
    "wrist_3_joint",
)
FORTRESS_REQUIRED_MAJOR = 6
FORTRESS_OBSERVED_VERSION = "6.18.0"
IGN_ROS2_CONTROL_PLUGIN = "libign_ros2_control-system.so"
FORBIDDEN_PLUGIN = "libgz_ros2_control-system.so"
EOAT_LINK = "real_aligned_eoat_visual_stack"
EOAT_FIXED_JOINT = "real_aligned_eoat_visual_stack_joint"
EOAT_CONTACT_COLLISION = "eoat_contact_pad_collision"
ACTIVE_TCP_LINK = "active_tcp"
ACTIVE_TCP_JOINT = "active_tcp_joint"
ACTIVE_TCP_OFFSET_TOOL0_M = (0.0000018186503701174852, 0.00000022293003722353485, 0.12209917288991741)
NATIVE_CONTACT_TOPIC = "/ur10e/gazebo_v2/native_contact"
NATIVE_FT_TOPIC = "/ur10e/gazebo_v2/native_ft"
TICK_SCHEMA = "ur10e_gazebo_v2_tick_v1"
ZERO6 = (0.0,) * 6
EOAT_GEOMETRY_FIDELITY = "provisional_v13_visual_plus_kunwei_collision_proxy"


@dataclass(frozen=True)
class BackendSpec:
    name: str
    command_interface: str
    controller_name: str
    controllers_filename: str
    fidelity: str


BACKENDS: Mapping[str, BackendSpec] = {
    "velocity": BackendSpec(
        name="velocity",
        command_interface="velocity",
        controller_name="gazebo_v2_velocity_controller",
        controllers_filename="gazebo_v2_velocity_controllers.yaml",
        fidelity="production_velocity_interface",
    ),
    "effort_surrogate": BackendSpec(
        name="effort_surrogate",
        command_interface="effort",
        controller_name="gazebo_v2_effort_surrogate_controller",
        controllers_filename="gazebo_v2_effort_surrogate_controllers.yaml",
        fidelity="simulator_only_effort_surrogate_not_true_ur_torque_backend",
    ),
}


def backend_spec(name: str) -> BackendSpec:
    try:
        return BACKENDS[name]
    except KeyError as exc:
        raise ValueError(f"unsupported Gazebo v2 backend: {name!r}") from exc


def configure_robot_description(robot_description: str, *, backend: str) -> str:
    """Return a Fortress-only, single-command-interface robot description."""

    spec = backend_spec(backend)
    root = ET.fromstring(robot_description)
    plugins = [str(node.attrib.get("filename") or "") for node in root.findall(".//plugin")]
    if not any(IGN_ROS2_CONTROL_PLUGIN in value for value in plugins):
        raise ValueError(f"robot description missing {IGN_ROS2_CONTROL_PLUGIN}")
    if any(FORBIDDEN_PLUGIN in value or "gz_ros2_control" in value for value in plugins):
        raise ValueError("Gazebo v2 robot description mixes gz_ros2_control with Fortress")
    if root.find(f"./link[@name='{EOAT_LINK}']") is None:
        raise ValueError(f"robot description missing attached EOAT link {EOAT_LINK}")
    if root.find(f"./joint[@name='{EOAT_FIXED_JOINT}']") is None:
        raise ValueError(f"robot description missing attached EOAT joint {EOAT_FIXED_JOINT}")
    if root.find(f"./link[@name='{ACTIVE_TCP_LINK}']") is None:
        raise ValueError(f"robot description missing active TCP link {ACTIVE_TCP_LINK}")
    active_tcp_joint = root.find(f"./joint[@name='{ACTIVE_TCP_JOINT}']")
    if active_tcp_joint is None:
        raise ValueError(f"robot description missing active TCP joint {ACTIVE_TCP_JOINT}")

    controls = root.findall("./ros2_control")
    if len(controls) != 1:
        raise ValueError(f"expected exactly one ros2_control block, found {len(controls)}")
    control = controls[0]
    by_name = {str(joint.attrib.get("name") or ""): joint for joint in control.findall("./joint")}
    if set(JOINT_NAMES) - set(by_name):
        missing = sorted(set(JOINT_NAMES) - set(by_name))
        raise ValueError(f"ros2_control block missing joints: {missing}")
    for name in JOINT_NAMES:
        joint = by_name[name]
        for command in list(joint.findall("./command_interface")):
            joint.remove(command)
        ET.SubElement(joint, "command_interface", {"name": spec.command_interface})

    _remove_existing_sensor_extensions(root)
    _append_native_contact_sensor(root)
    _append_native_ft_sensor(root)
    configured = ET.tostring(root, encoding="unicode")
    audit = audit_robot_description(configured, backend=backend)
    if not audit["pass"]:
        raise ValueError("invalid Gazebo v2 robot description: " + ";".join(audit["blockers"]))
    return configured


def _remove_existing_sensor_extensions(root: ET.Element) -> None:
    for extension in list(root.findall("./gazebo")):
        sensor_names = {str(sensor.attrib.get("name") or "") for sensor in extension.findall("./sensor")}
        if sensor_names & {"gazebo_v2_native_contact", "gazebo_v2_native_ft"}:
            root.remove(extension)


def _append_native_contact_sensor(root: ET.Element) -> None:
    extension = ET.SubElement(root, "gazebo", {"reference": EOAT_LINK})
    sensor = ET.SubElement(extension, "sensor", {"name": "gazebo_v2_native_contact", "type": "contact"})
    ET.SubElement(sensor, "always_on").text = "true"
    ET.SubElement(sensor, "update_rate").text = "500"
    ET.SubElement(sensor, "topic").text = NATIVE_CONTACT_TOPIC
    contact = ET.SubElement(sensor, "contact")
    ET.SubElement(contact, "collision").text = EOAT_CONTACT_COLLISION


def _append_native_ft_sensor(root: ET.Element) -> None:
    extension = ET.SubElement(root, "gazebo", {"reference": EOAT_FIXED_JOINT})
    ET.SubElement(extension, "preserveFixedJoint").text = "true"
    sensor = ET.SubElement(extension, "sensor", {"name": "gazebo_v2_native_ft", "type": "force_torque"})
    ET.SubElement(sensor, "always_on").text = "true"
    ET.SubElement(sensor, "update_rate").text = "500"
    ET.SubElement(sensor, "topic").text = NATIVE_FT_TOPIC
    settings = ET.SubElement(sensor, "force_torque")
    ET.SubElement(settings, "frame").text = "child"
    ET.SubElement(settings, "measure_direction").text = "child_to_parent"


def audit_robot_description(robot_description: str, *, backend: str) -> dict[str, Any]:
    spec = backend_spec(backend)
    root = ET.fromstring(robot_description)
    blockers: list[str] = []
    plugins = [str(node.attrib.get("filename") or "") for node in root.findall(".//plugin")]
    if not any(IGN_ROS2_CONTROL_PLUGIN in value for value in plugins):
        blockers.append("fortress_ros2_control_plugin_missing")
    if any(FORBIDDEN_PLUGIN in value or "gz_ros2_control" in value for value in plugins):
        blockers.append("mixed_gz_ros2_control_plugin")

    controls = root.findall("./ros2_control")
    interface_inventory: dict[str, list[str]] = {}
    if len(controls) != 1:
        blockers.append("ros2_control_block_count_not_one")
    else:
        for joint in controls[0].findall("./joint"):
            name = str(joint.attrib.get("name") or "")
            if name not in JOINT_NAMES:
                continue
            interface_inventory[name] = [
                str(command.attrib.get("name") or "") for command in joint.findall("./command_interface")
            ]
        for name in JOINT_NAMES:
            if interface_inventory.get(name) != [spec.command_interface]:
                blockers.append(f"joint_command_interface_not_exclusive:{name}")

    contact_extensions = [
        extension
        for extension in root.findall("./gazebo")
        if extension.attrib.get("reference") == EOAT_LINK
        and extension.find("./sensor[@name='gazebo_v2_native_contact']") is not None
    ]
    ft_extensions = [
        extension
        for extension in root.findall("./gazebo")
        if extension.attrib.get("reference") == EOAT_FIXED_JOINT
        and extension.find("./sensor[@name='gazebo_v2_native_ft']") is not None
    ]
    if len(contact_extensions) != 1:
        blockers.append("attached_native_contact_sensor_count_not_one")
    else:
        sensor = contact_extensions[0].find("./sensor[@name='gazebo_v2_native_contact']")
        if _text(sensor, "./topic") != NATIVE_CONTACT_TOPIC:
            blockers.append("native_contact_topic_mismatch")
        if _text(sensor, "./contact/collision") != EOAT_CONTACT_COLLISION:
            blockers.append("native_contact_collision_mismatch")
        if _text(sensor, "./update_rate") != "500":
            blockers.append("native_contact_rate_not_500")
    if len(ft_extensions) != 1:
        blockers.append("attached_native_ft_sensor_count_not_one")
    else:
        extension = ft_extensions[0]
        sensor = extension.find("./sensor[@name='gazebo_v2_native_ft']")
        if _text(extension, "./preserveFixedJoint") != "true":
            blockers.append("native_ft_fixed_joint_not_preserved")
        if _text(sensor, "./topic") != NATIVE_FT_TOPIC:
            blockers.append("native_ft_topic_mismatch")
        if _text(sensor, "./update_rate") != "500":
            blockers.append("native_ft_rate_not_500")
        if _text(sensor, "./force_torque/frame") != "child":
            blockers.append("native_ft_frame_not_child")
        if _text(sensor, "./force_torque/measure_direction") != "child_to_parent":
            blockers.append("native_ft_direction_mismatch")

    active_tcp_joint = root.find(f"./joint[@name='{ACTIVE_TCP_JOINT}']")
    active_tcp_origin = active_tcp_joint.find("./origin") if active_tcp_joint is not None else None
    active_tcp_extension = root.find(f"./gazebo[@reference='{ACTIVE_TCP_JOINT}']")
    if root.find(f"./link[@name='{ACTIVE_TCP_LINK}']") is None:
        blockers.append("active_tcp_link_missing")
    if active_tcp_joint is None:
        blockers.append("active_tcp_joint_missing")
    else:
        parent = active_tcp_joint.find("./parent")
        child = active_tcp_joint.find("./child")
        if parent is None or parent.attrib.get("link") != "tool0":
            blockers.append("active_tcp_parent_not_tool0")
        if child is None or child.attrib.get("link") != ACTIVE_TCP_LINK:
            blockers.append("active_tcp_child_mismatch")
        if active_tcp_origin is None or active_tcp_origin.attrib.get("rpy") != "0 0 0":
            blockers.append("active_tcp_rotation_mismatch")
        else:
            try:
                xyz = tuple(float(value) for value in active_tcp_origin.attrib.get("xyz", "").split())
            except ValueError:
                xyz = ()
            if len(xyz) != 3 or any(
                abs(actual - expected) > 1e-15 for actual, expected in zip(xyz, ACTIVE_TCP_OFFSET_TOOL0_M)
            ):
                blockers.append("active_tcp_offset_mismatch")
    if active_tcp_extension is None or _text(active_tcp_extension, "./preserveFixedJoint") != "true":
        blockers.append("active_tcp_fixed_joint_not_preserved")

    return {
        "schema": "ur10e_gazebo_v2_robot_description_audit_v1",
        "pass": not blockers,
        "backend": backend,
        "backend_fidelity": spec.fidelity,
        "command_interface": spec.command_interface,
        "command_interface_inventory": interface_inventory,
        "contact_sensor_attachment": {"link": EOAT_LINK, "collision": EOAT_CONTACT_COLLISION},
        "ft_sensor_attachment": {"joint": EOAT_FIXED_JOINT, "frame": "child", "direction": "child_to_parent"},
        "active_tcp": {
            "link": ACTIVE_TCP_LINK,
            "joint": ACTIVE_TCP_JOINT,
            "parent": "tool0",
            "offset_tool0_m": list(ACTIVE_TCP_OFFSET_TOOL0_M),
        },
        "geometry": {
            "fidelity": EOAT_GEOMETRY_FIDELITY,
            "current_bench_cad_hash_bound": False,
            "mass_cog_inertia_calibrated": False,
            "active_tcp_offset_z_m": 0.12209917288991741,
        },
        "blockers": blockers,
        "claim_boundary": {
            "true_ur_torque_control": False,
            "real_robot_or_live_acceptance": False,
            "native_gazebo_contact_ft_requires_runtime_evidence": True,
            "current_bench_geometry_equivalence": False,
            "p0_simulator_physics_acceptance": False,
        },
    }


def _text(node: ET.Element | None, path: str) -> str:
    if node is None:
        return ""
    child = node.find(path)
    return str(child.text or "").strip() if child is not None else ""


def parse_fortress_version(text: str) -> tuple[int, int, int] | None:
    match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", text)
    if not match:
        return None
    return tuple(int(value) for value in match.groups())  # type: ignore[return-value]


def build_abi_preflight(
    *,
    ign_path: str | None,
    version_output: str,
    ign_plugin_paths: Sequence[str],
    ros_gz_sim_prefix: str | None,
) -> dict[str, Any]:
    version = parse_fortress_version(version_output)
    plugin_names = [Path(path).name for path in ign_plugin_paths]
    blockers: list[str] = []
    if not ign_path:
        blockers.append("ign_executable_missing")
    if version is None:
        blockers.append("ign_gazebo_version_unparseable")
    elif version[0] != FORTRESS_REQUIRED_MAJOR:
        blockers.append(f"ign_gazebo_major_not_{FORTRESS_REQUIRED_MAJOR}")
    if IGN_ROS2_CONTROL_PLUGIN not in plugin_names:
        blockers.append("ign_ros2_control_plugin_missing")
    if FORBIDDEN_PLUGIN in plugin_names:
        blockers.append("gz_ros2_control_plugin_selected")
    if not ros_gz_sim_prefix:
        blockers.append("ros_gz_sim_spawn_package_missing")
    return {
        "schema": "ur10e_gazebo_v2_abi_preflight_v1",
        "pass": not blockers,
        "engine_family": "Gazebo Fortress",
        "ign_path": ign_path,
        "version": list(version) if version is not None else None,
        "required_major": FORTRESS_REQUIRED_MAJOR,
        "observed_reference_version": FORTRESS_OBSERVED_VERSION,
        "ros2_control_plugin": IGN_ROS2_CONTROL_PLUGIN,
        "plugin_paths": list(ign_plugin_paths),
        "spawn_package": "ros_gz_sim",
        "spawn_package_prefix": ros_gz_sim_prefix,
        "forbidden_runtime": ["gz sim", "gz_ros2_control", FORBIDDEN_PLUGIN],
        "blockers": blockers,
    }


def probe_fortress_abi(
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    ign_path = shutil.which("ign")
    version_output = ""
    if ign_path:
        result = runner([ign_path, "gazebo", "--versions"], capture_output=True, text=True, check=False)
        version_output = (result.stdout or "") + "\n" + (result.stderr or "")
    plugin_paths: list[str] = []
    search_roots = [
        Path("/opt/ros/humble/lib"),
        Path("/usr/lib/x86_64-linux-gnu/ign-gazebo-6/plugins"),
    ]
    for entry in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep):
        if entry:
            search_roots.append(Path(entry))
    for root in search_roots:
        candidate = root / IGN_ROS2_CONTROL_PLUGIN
        if candidate.exists():
            plugin_paths.append(str(candidate))
    plugin_paths = list(dict.fromkeys(plugin_paths))
    ros_gz_sim_prefix: str | None = None
    ros2_path = shutil.which("ros2")
    if ros2_path:
        result = runner(
            [ros2_path, "pkg", "prefix", "ros_gz_sim"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            ros_gz_sim_prefix = result.stdout.strip()
    return build_abi_preflight(
        ign_path=ign_path,
        version_output=version_output,
        ign_plugin_paths=plugin_paths,
        ros_gz_sim_prefix=ros_gz_sim_prefix,
    )


def validate_tick_record(record: Mapping[str, Any]) -> list[str]:
    """Validate the semantic constraints not expressible in JSON Schema."""

    issues: list[str] = []
    if record.get("schema") != TICK_SCHEMA:
        issues.append("schema_mismatch")
    if not str(record.get("run_id") or ""):
        issues.append("run_id_missing")
    sequence = record.get("sequence")
    if not isinstance(sequence, int) or sequence < 0:
        issues.append("sequence_invalid")
    if not _finite_number(record.get("sim_time_s")) or float(record.get("sim_time_s") or 0.0) < 0.0:
        issues.append("sim_time_invalid")
    try:
        spec = backend_spec(str(record.get("backend") or ""))
    except ValueError:
        spec = None
        issues.append("backend_invalid")

    for section, fields in {
        "state": ("q_rad", "qd_rad_s", "tcp_pose_base", "tcp_twist_base", "native_ft_tcp"),
        "candidate": ("values",),
        "command": ("values",),
    }.items():
        payload = record.get(section)
        if not isinstance(payload, Mapping):
            issues.append(f"{section}_missing")
            continue
        for field in fields:
            if not _finite_vector6(payload.get(field)):
                issues.append(f"{section}.{field}_invalid")

    state = record.get("state") if isinstance(record.get("state"), Mapping) else {}
    frame_lineage = state.get("frame_lineage") if isinstance(state, Mapping) else None
    if not isinstance(frame_lineage, Mapping):
        issues.append("state.frame_lineage_missing")
    else:
        required_frames = {"world", "spawn_root", "control_base", "tool", "active_tcp"}
        if required_frames - set(frame_lineage):
            issues.append("state.frame_lineage_incomplete")
    if not isinstance(state.get("native_contact_count"), int) or int(state.get("native_contact_count") or 0) < 0:
        issues.append("state.native_contact_count_invalid")

    candidate = record.get("candidate") if isinstance(record.get("candidate"), Mapping) else {}
    source = str(candidate.get("source") or "")
    if not source:
        issues.append("candidate.source_missing")
    if "virtual_surface" in source.lower():
        issues.append("candidate.virtual_surface_force_forbidden")
    if not isinstance(candidate.get("valid"), bool):
        issues.append("candidate.valid_invalid")
    if not isinstance(candidate.get("diagnostics"), Mapping):
        issues.append("candidate.diagnostics_invalid")

    decision = record.get("decision") if isinstance(record.get("decision"), Mapping) else {}
    action = decision.get("action")
    accepted = decision.get("accepted")
    if action not in {"accept", "hold", "stop"}:
        issues.append("decision.action_invalid")
    if not str(decision.get("reason") or ""):
        issues.append("decision.reason_missing")
    if not isinstance(accepted, bool) or accepted != (action == "accept"):
        issues.append("decision.accepted_inconsistent")

    command = record.get("command") if isinstance(record.get("command"), Mapping) else {}
    expected_interface = spec.command_interface if spec is not None else None
    if command.get("interface") != expected_interface:
        issues.append("command.interface_backend_mismatch")
    if command.get("sequence") != sequence:
        issues.append("command.sequence_mismatch")
    if not isinstance(command.get("applied"), bool):
        issues.append("command.applied_invalid")
    command_values = command.get("values")
    if action in {"hold", "stop"} and _finite_vector6(command_values):
        if tuple(float(value) for value in command_values) != ZERO6:
            issues.append("rejected_command_not_exact_zero")
    if action == "accept" and candidate.get("valid") is not True:
        issues.append("accepted_candidate_not_valid")
    if action == "accept" and command.get("applied") is not True:
        issues.append("accepted_command_not_applied")
    if action == "accept" and _finite_vector6(command_values) and _finite_vector6(candidate.get("values")):
        if tuple(float(value) for value in command_values) != tuple(
            float(value) for value in candidate.get("values", ())
        ):
            issues.append("accepted_command_candidate_mismatch")
    return issues


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _finite_vector6(value: Any) -> bool:
    return (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
        and len(value) == 6
        and all(_finite_number(item) for item in value)
    )
