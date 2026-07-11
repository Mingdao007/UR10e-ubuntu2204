#!/usr/bin/env python3
"""Build hash-bound calibrated UR10e MuJoCo plants for offline simulation.

The robot is compiled from the controller-calibrated UR xacro/YAML.  The
legacy nominal MJCF and guessed 85 mm TCP are hard-denied.  The currently
known Kunwei stack is emitted as an explicitly provisional geometry/inertia
variant; it cannot support a calibrated-physics or live claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = EXPERIMENT_ROOT.parents[1]
DEFAULT_INPUTS = EXPERIMENT_ROOT / "config" / "digital_twin_model_inputs_v1.json"
JOINT_NAMES = (
    "shoulder_pan_joint",
    "shoulder_lift_joint",
    "elbow_joint",
    "wrist_1_joint",
    "wrist_2_joint",
    "wrist_3_joint",
)
VELOCITY_MODEL = "ur10e_p0_velocity.xml"
TORQUE_MODEL = "ur10e_vic_torque_surrogate.xml"


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def binding(path: Path, *, role: str, claim_level: str) -> dict[str, object]:
    return {
        "role": role,
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_path(path),
        "claim_level": claim_level,
    }


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object in {path}")
    return value


def _resolve_repo_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


def validate_inputs(config: Mapping[str, Any]) -> None:
    if config.get("schema") != "ur10e_digital_twin_model_inputs_v1":
        raise ValueError("digital twin model input schema is invalid")
    rates = config.get("rates_hz") or {}
    if rates != {"physics": 2000, "control": 500, "dbil": 200}:
        raise ValueError("digital twin requires exact 2000/500/200 Hz rates")
    if int(rates["physics"]) % int(rates["control"]) or int(rates["physics"]) % int(rates["dbil"]):
        raise ValueError("digital twin rates must form an integer schedule")
    robot = config.get("robot") or {}
    calibration = _resolve_repo_path(str(robot.get("calibration_yaml") or ""))
    initial = _resolve_repo_path(str(robot.get("initial_positions_yaml") or ""))
    for label, path, expected in (
        ("calibration", calibration, robot.get("calibration_yaml_sha256")),
        ("initial_positions", initial, robot.get("initial_positions_yaml_sha256")),
    ):
        if not path.is_file():
            raise FileNotFoundError(f"{label} input missing: {path}")
        if sha256_path(path) != expected:
            raise ValueError(f"{label} input hash mismatch")
    surface = config.get("surface") or {}
    surface_mesh = _resolve_repo_path(str(surface.get("mesh") or ""))
    if not surface_mesh.is_file() or sha256_path(surface_mesh) != surface.get("mesh_sha256"):
        raise ValueError("surface mesh hash mismatch")
    variant_id = str(config.get("active_variant") or "")
    variant = (config.get("variants") or {}).get(variant_id)
    if not isinstance(variant, Mapping):
        raise ValueError("active EOAT variant is missing")
    if variant.get("claim_ceiling") != "geometry_provisional":
        raise ValueError("current build must remain geometry_provisional")
    claim = config.get("claim_boundary") or {}
    for field in (
        "calibrated_physics_claim_allowed",
        "p0_sim_physics_pass_allowed",
        "live_motion_authorized",
        "package_accepted",
        "live_accepted",
        "reproduction_complete",
    ):
        if claim.get(field) is not False:
            raise ValueError(f"model input claim boundary crosses {field}")


def rpy_matrix(rpy: Sequence[float]) -> np.ndarray:
    roll, pitch, yaw = (float(value) for value in rpy)
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    rx = np.array(((1.0, 0.0, 0.0), (0.0, cr, -sr), (0.0, sr, cr)))
    ry = np.array(((cp, 0.0, sp), (0.0, 1.0, 0.0), (-sp, 0.0, cp)))
    rz = np.array(((cy, -sy, 0.0), (sy, cy, 0.0), (0.0, 0.0, 1.0)))
    return rz @ ry @ rx


def matrix_to_quat_wxyz(rotation: np.ndarray) -> tuple[float, float, float, float]:
    if rotation.shape != (3, 3) or not np.all(np.isfinite(rotation)):
        raise ValueError("rotation must be finite 3x3")
    trace = float(np.trace(rotation))
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        quat = np.array(
            (0.25 * scale, (rotation[2, 1] - rotation[1, 2]) / scale, (rotation[0, 2] - rotation[2, 0]) / scale, (rotation[1, 0] - rotation[0, 1]) / scale)
        )
    else:
        index = int(np.argmax(np.diag(rotation)))
        if index == 0:
            scale = math.sqrt(max(0.0, 1.0 + rotation[0, 0] - rotation[1, 1] - rotation[2, 2])) * 2.0
            quat = np.array(((rotation[2, 1] - rotation[1, 2]) / scale, 0.25 * scale, (rotation[0, 1] + rotation[1, 0]) / scale, (rotation[0, 2] + rotation[2, 0]) / scale))
        elif index == 1:
            scale = math.sqrt(max(0.0, 1.0 + rotation[1, 1] - rotation[0, 0] - rotation[2, 2])) * 2.0
            quat = np.array(((rotation[0, 2] - rotation[2, 0]) / scale, (rotation[0, 1] + rotation[1, 0]) / scale, 0.25 * scale, (rotation[1, 2] + rotation[2, 1]) / scale))
        else:
            scale = math.sqrt(max(0.0, 1.0 + rotation[2, 2] - rotation[0, 0] - rotation[1, 1])) * 2.0
            quat = np.array(((rotation[1, 0] - rotation[0, 1]) / scale, (rotation[0, 2] + rotation[2, 0]) / scale, (rotation[1, 2] + rotation[2, 1]) / scale, 0.25 * scale))
    quat /= float(np.linalg.norm(quat))
    return tuple(float(value) for value in quat)  # type: ignore[return-value]


def _origin_transform(origin: ET.Element | None) -> np.ndarray:
    transform = np.eye(4)
    if origin is None:
        return transform
    xyz = [float(value) for value in origin.attrib.get("xyz", "0 0 0").split()]
    rpy = [float(value) for value in origin.attrib.get("rpy", "0 0 0").split()]
    transform[:3, :3] = rpy_matrix(rpy)
    transform[:3, 3] = xyz
    return transform


def fixed_transform(urdf_text: str, parent: str, child: str) -> np.ndarray:
    root = ET.fromstring(urdf_text)
    edges: dict[str, tuple[str, np.ndarray]] = {}
    for joint in root.findall("joint"):
        if joint.attrib.get("type") != "fixed":
            continue
        parent_node = joint.find("parent")
        child_node = joint.find("child")
        if parent_node is None or child_node is None:
            continue
        edges[child_node.attrib["link"]] = (
            parent_node.attrib["link"],
            _origin_transform(joint.find("origin")),
        )
    chain: list[np.ndarray] = []
    cursor = child
    while cursor != parent:
        if cursor not in edges:
            raise ValueError(f"no fixed transform chain from {parent} to {child}")
        next_parent, transform = edges[cursor]
        chain.append(transform)
        cursor = next_parent
    result = np.eye(4)
    for transform in reversed(chain):
        result = result @ transform
    return result


def box_diagonal_inertia(mass_kg: float, size_xyz_m: Sequence[float]) -> tuple[float, float, float]:
    x, y, z = (float(value) for value in size_xyz_m)
    mass = float(mass_kg)
    return (
        mass * (y * y + z * z) / 12.0,
        mass * (x * x + z * z) / 12.0,
        mass * (x * x + y * y) / 12.0,
    )


def _add_geom(
    body: ET.Element,
    *,
    name: str,
    kind: str,
    pos: Sequence[float],
    size: Sequence[float],
    rgba: str,
    collision: bool = True,
) -> ET.Element:
    return ET.SubElement(
        body,
        "geom",
        {
            "name": name,
            "type": kind,
            "pos": " ".join(f"{float(value):.12g}" for value in pos),
            "size": " ".join(f"{float(value):.12g}" for value in size),
            "rgba": rgba,
            "contype": "1" if collision else "0",
            "conaffinity": "1" if collision else "0",
            "group": "3" if collision else "1",
            "friction": "0.8 0.01 0.001",
        },
    )


def _look_at_quat(position: Sequence[float], target: Sequence[float]) -> tuple[float, float, float, float]:
    position_array = np.asarray(position, dtype=float)
    target_array = np.asarray(target, dtype=float)
    forward = target_array - position_array
    forward /= float(np.linalg.norm(forward))
    # MuJoCo cameras look along local -Z with local +Y as up.
    z_axis = -forward
    up = np.array((0.0, 0.0, 1.0))
    x_axis = np.cross(up, z_axis)
    if float(np.linalg.norm(x_axis)) < 1e-9:
        x_axis = np.array((1.0, 0.0, 0.0))
    x_axis /= float(np.linalg.norm(x_axis))
    y_axis = np.cross(z_axis, x_axis)
    rotation = np.column_stack((x_axis, y_axis, z_axis))
    return matrix_to_quat_wxyz(rotation)


def patch_mjcf(
    canonical_mjcf: str,
    *,
    config: Mapping[str, Any],
    urdf_text: str,
    actuator_mode: str,
) -> str:
    if actuator_mode not in {"velocity", "torque_surrogate"}:
        raise ValueError("actuator_mode must be velocity or torque_surrogate")
    root = ET.fromstring(canonical_mjcf)
    root.attrib["model"] = f"ur10e_digital_twin_{actuator_mode}"
    compiler = root.find("compiler")
    if compiler is None:
        compiler = ET.SubElement(root, "compiler")
    compiler.attrib.update({"angle": "radian", "autolimits": "true", "balanceinertia": "true"})
    option = root.find("option")
    if option is None:
        option = ET.SubElement(root, "option")
    option.attrib.update(
        {
            "timestep": "0.0005",
            "gravity": "0 0 -9.81",
            "integrator": "implicitfast",
            "cone": "elliptic",
            "jacobian": "dense",
        }
    )
    visual = root.find("visual")
    if visual is None:
        visual = ET.SubElement(root, "visual")
    ET.SubElement(visual, "global", {"offwidth": "1280", "offheight": "720"})
    ET.SubElement(visual, "quality", {"shadowsize": "4096", "offsamples": "4"})

    worldbody = root.find("worldbody")
    asset = root.find("asset")
    if worldbody is None or asset is None:
        raise ValueError("canonical MuJoCo model is missing worldbody/asset")
    for index, geom in enumerate(worldbody.iter("geom")):
        geom.attrib.setdefault("name", f"ur_collision_{index}")
        geom.attrib.setdefault("rgba", "0.72 0.74 0.76 1")
        geom.attrib.setdefault("group", "2")
        geom.attrib.setdefault("contype", "1")
        geom.attrib.setdefault("conaffinity", "1")
    for body in worldbody.iter("body"):
        body.attrib["gravcomp"] = "1"
    for joint in worldbody.iter("joint"):
        joint.attrib.setdefault("damping", "0.1")
        joint.attrib.setdefault("armature", "0.01")

    wrist = worldbody.find(".//body[@name='wrist_3_link']")
    if wrist is None:
        raise ValueError("compiled model is missing wrist_3_link")
    wrist_to_tool0 = fixed_transform(urdf_text, "wrist_3_link", "tool0")
    tool0 = ET.SubElement(
        wrist,
        "body",
        {
            "name": "tool0",
            "pos": " ".join(f"{value:.12g}" for value in wrist_to_tool0[:3, 3]),
            "quat": " ".join(f"{value:.12g}" for value in matrix_to_quat_wxyz(wrist_to_tool0[:3, :3])),
            "gravcomp": "1",
        },
    )
    variant = config["variants"][config["active_variant"]]
    mass = float(variant["mass_nominal_kg"])
    cog = [float(value) for value in variant["cog_nominal_tool0_m"]]
    inertia = box_diagonal_inertia(mass, (0.10, 0.10, 0.1221))
    eoat = ET.SubElement(tool0, "body", {"name": "current_kunwei_eoat", "gravcomp": "1"})
    ET.SubElement(
        eoat,
        "inertial",
        {
            "pos": " ".join(f"{value:.12g}" for value in cog),
            "mass": f"{mass:.12g}",
            "diaginertia": " ".join(f"{value:.12g}" for value in inertia),
        },
    )
    _add_geom(eoat, name="eoat_flange_adapter_collision", kind="cylinder", pos=(0, 0, 0.012), size=(0.058, 0.012), rgba="0.32 0.33 0.34 1")
    _add_geom(eoat, name="eoat_kunwei_sensor_body_collision", kind="cylinder", pos=(0, 0, 0.052), size=(0.043, 0.030), rgba="0.18 0.20 0.22 1")
    _add_geom(eoat, name="eoat_left_bracket_collision", kind="box", pos=(0, 0.046, 0.086), size=(0.050, 0.007, 0.020), rgba="0.55 0.56 0.57 1")
    _add_geom(eoat, name="eoat_right_bracket_collision", kind="box", pos=(0, -0.046, 0.086), size=(0.050, 0.007, 0.020), rgba="0.55 0.56 0.57 1")
    _add_geom(eoat, name="eoat_tool_plate_collision", kind="box", pos=(0, 0, 0.092), size=(0.059, 0.037, 0.008), rgba="0.43 0.44 0.45 1")
    _add_geom(eoat, name="eoat_contact_probe_collision", kind="cylinder", pos=(0, 0, 0.106), size=(0.014, 0.026), rgba="0.48 0.49 0.50 1")
    _add_geom(eoat, name="eoat_contact_pad_collision", kind="box", pos=(0, 0, 0.122), size=(0.026, 0.026, 0.005), rgba="0.64 0.65 0.66 1")
    tcp = [float(value) for value in variant["active_tcp_offset_tool0_m"]]
    ET.SubElement(eoat, "site", {"name": "active_tcp_site", "pos": " ".join(f"{value:.12g}" for value in tcp), "size": "0.004", "rgba": "0.9 0.2 0.1 1", "group": "4"})
    ET.SubElement(eoat, "site", {"name": "kunwei_ft_sensor_site", "pos": "0 0 0.022", "size": "0.006", "rgba": "0.2 0.4 0.9 0.35", "group": "4"})

    surface = config["surface"]
    ET.SubElement(asset, "mesh", {"name": "step5_surface_mesh", "file": "assets/contact_surface/two_piece_surface_smooth_v11_3mm_thick.stl", "scale": "0.001 0.001 0.001"})
    ET.SubElement(worldbody, "geom", {"name": "ground_plane", "type": "plane", "pos": "0 0 -0.08", "size": "2 2 0.02", "rgba": "0.70 0.68 0.63 1", "contype": "1", "conaffinity": "1"})
    ET.SubElement(worldbody, "geom", {"name": "work_table", "type": "box", "pos": "0.46 0.13 -0.045", "size": "0.40 0.32 0.035", "rgba": "0.42 0.38 0.33 1", "contype": "1", "conaffinity": "1"})
    surface_body = ET.SubElement(worldbody, "body", {"name": "step5_surface", "pos": " ".join(str(value) for value in surface["world_pose_xyz_m"])})
    mesh_pose = surface["mesh_pose_xyz_rpy"]
    mesh_quat = matrix_to_quat_wxyz(rpy_matrix(mesh_pose[3:6]))
    ET.SubElement(
        surface_body,
        "geom",
        {
            "name": "step5_surface_collision",
            "type": "mesh",
            "mesh": "step5_surface_mesh",
            "pos": " ".join(str(value) for value in mesh_pose[:3]),
            "quat": " ".join(f"{value:.12g}" for value in mesh_quat),
            "rgba": "0.10 0.46 0.49 1",
            "contype": "1",
            "conaffinity": "1",
            "friction": "1.0 0.01 0.001",
            "solref": "0.002 1",
            "solimp": "0.95 0.99 0.001",
        },
    )

    for name, position, target in (
        ("wide", (1.35, -1.05, 0.95), (0.35, 0.10, 0.25)),
        ("oblique", (0.95, 0.85, 0.65), (0.38, 0.10, 0.20)),
        ("close", (0.58, -0.30, 0.34), (0.35, 0.08, 0.16)),
        ("contact", (0.53, 0.00, 0.16), (0.45, 0.12, 0.05)),
    ):
        ET.SubElement(worldbody, "camera", {"name": f"observer_{name}", "pos": " ".join(str(value) for value in position), "quat": " ".join(f"{value:.12g}" for value in _look_at_quat(position, target)), "fovy": "42" if name != "contact" else "34"})

    contact = ET.SubElement(root, "contact")
    ET.SubElement(contact, "pair", {"name": "eoat_surface_pair", "geom1": "eoat_contact_pad_collision", "geom2": "step5_surface_collision", "condim": "4", "friction": "1.0 0.01 0.001", "solref": "0.002 1", "solimp": "0.95 0.99 0.001"})

    actuator = ET.SubElement(root, "actuator")
    torque_limits = (330.0, 330.0, 150.0, 54.0, 54.0, 54.0)
    for index, joint_name in enumerate(JOINT_NAMES):
        if actuator_mode == "velocity":
            ET.SubElement(actuator, "velocity", {"name": f"velocity_{joint_name}", "joint": joint_name, "kv": "120", "ctrlrange": "-0.05 0.05", "forcerange": f"{-torque_limits[index]:.12g} {torque_limits[index]:.12g}"})
        else:
            ET.SubElement(actuator, "motor", {"name": f"torque_{joint_name}", "joint": joint_name, "gear": "1", "ctrlrange": f"{-torque_limits[index]:.12g} {torque_limits[index]:.12g}"})

    sensor = ET.SubElement(root, "sensor")
    for joint_name in JOINT_NAMES:
        ET.SubElement(sensor, "jointpos", {"name": f"q_{joint_name}", "joint": joint_name})
        ET.SubElement(sensor, "jointvel", {"name": f"qd_{joint_name}", "joint": joint_name})
    ET.SubElement(sensor, "framepos", {"name": "active_tcp_position", "objtype": "site", "objname": "active_tcp_site"})
    ET.SubElement(sensor, "framequat", {"name": "active_tcp_quaternion", "objtype": "site", "objname": "active_tcp_site"})
    ET.SubElement(sensor, "framelinvel", {"name": "active_tcp_linear_velocity", "objtype": "site", "objname": "active_tcp_site"})
    ET.SubElement(sensor, "frameangvel", {"name": "active_tcp_angular_velocity", "objtype": "site", "objname": "active_tcp_site"})
    ET.SubElement(sensor, "force", {"name": "kunwei_force", "site": "kunwei_ft_sensor_site"})
    ET.SubElement(sensor, "torque", {"name": "kunwei_torque", "site": "kunwei_ft_sensor_site"})
    ET.SubElement(sensor, "touch", {"name": "contact_pad_touch", "site": "active_tcp_site"})

    initial = config["initial_q"]
    keyframe = ET.SubElement(root, "keyframe")
    ET.SubElement(keyframe, "key", {"name": "p0_initial", "qpos": " ".join(f"{float(value):.16g}" for value in initial)})
    return ET.tostring(root, encoding="unicode")


def _referenced_meshes(urdf_text: str) -> list[Path]:
    root = ET.fromstring(urdf_text)
    paths: list[Path] = []
    for mesh in root.findall(".//mesh"):
        filename = mesh.attrib.get("filename")
        if filename:
            paths.append(Path(filename))
    return sorted(set(paths))


def _vendor_assets(canonical_root: ET.Element, output_dir: Path, urdf_meshes: Iterable[Path], surface_mesh: Path) -> list[dict[str, object]]:
    assets: list[dict[str, object]] = []
    destination_root = output_dir / "assets" / "ur_description"
    destination_root.mkdir(parents=True, exist_ok=True)
    source_by_stem = {path.stem: path for path in urdf_meshes}
    for mesh in canonical_root.findall("./asset/mesh"):
        name = str(mesh.attrib.get("name") or "")
        source = Path(str(mesh.attrib.get("file") or source_by_stem.get(name) or ""))
        if not source.is_file():
            source = source_by_stem.get(name, source)
        if not source.is_file():
            raise FileNotFoundError(f"MuJoCo source mesh missing: {name} {source}")
        destination = destination_root / f"{name}{source.suffix.lower()}"
        shutil.copy2(source, destination)
        mesh.attrib["file"] = str(destination.relative_to(output_dir))
        assets.append(binding(destination, role=f"vendored_ur_mesh:{name}", claim_level="calibrated_robot_geometry"))
    surface_destination = output_dir / "assets" / "contact_surface" / surface_mesh.name
    surface_destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(surface_mesh, surface_destination)
    assets.append(binding(surface_destination, role="vendored_contact_surface_mesh", claim_level="geometry_only"))
    return assets


def _validate_model(model: Any, *, mode: str) -> dict[str, object]:
    joint_names = [model.joint(index).name for index in range(model.njnt)]
    if tuple(joint_names) != JOINT_NAMES:
        raise ValueError(f"MuJoCo joint ordering mismatch: {joint_names}")
    if model.nq != 6 or model.nv != 6 or model.nu != 6:
        raise ValueError(f"MuJoCo model shape mismatch nq={model.nq} nv={model.nv} nu={model.nu}")
    if not math.isclose(float(model.opt.timestep), 0.0005, abs_tol=1e-12):
        raise ValueError("MuJoCo physics timestep is not 0.5 ms")
    required_sites = {"active_tcp_site", "kunwei_ft_sensor_site"}
    sites = {model.site(index).name for index in range(model.nsite)}
    if not required_sites.issubset(sites):
        raise ValueError("MuJoCo model is missing TCP/FT sites")
    actuator_names = [model.actuator(index).name for index in range(model.nu)]
    expected_prefix = "velocity_" if mode == "velocity" else "torque_"
    if any(not name.startswith(expected_prefix) for name in actuator_names):
        raise ValueError("MuJoCo actuator modes are not mutually exclusive")
    sensor_names = {model.sensor(index).name for index in range(model.nsensor)}
    if not {"kunwei_force", "kunwei_torque", "contact_pad_touch"}.issubset(sensor_names):
        raise ValueError("MuJoCo model is missing force/torque/contact sensors")
    return {
        "nq": model.nq,
        "nv": model.nv,
        "nbody": model.nbody,
        "njnt": model.njnt,
        "ngeom": model.ngeom,
        "nsite": model.nsite,
        "nsensor": model.nsensor,
        "nu": model.nu,
        "joint_order": joint_names,
        "actuators": actuator_names,
        "sites": sorted(sites),
        "sensors": sorted(sensor_names),
        "physics_timestep_s": float(model.opt.timestep),
    }


def build(
    *,
    inputs_path: Path,
    output_dir: Path,
    generated_at: str,
) -> dict[str, Any]:
    import mujoco
    import pinocchio
    import xacro
    import yaml

    import step5c_calibrated_kinematics_audit as kinematics

    config = _load_json(inputs_path)
    validate_inputs(config)
    robot = config["robot"]
    calibration = _resolve_repo_path(robot["calibration_yaml"])
    xacro_path = Path(robot["xacro_path"])
    if not xacro_path.is_file():
        raise FileNotFoundError(f"UR xacro missing: {xacro_path}")
    urdf_text, calibration_hash = kinematics.generate_calibrated_urdf(calibration, xacro_path)
    if calibration_hash != robot["calibration_hash"]:
        raise ValueError("calibrated URDF logical hash mismatch")
    package_root = xacro_path.parents[1]
    resolved_urdf = urdf_text.replace("package://ur_description/", f"{package_root}/")
    if "package://" in resolved_urdf:
        raise ValueError("calibrated URDF retains unresolved package URI")
    initial_payload = yaml.safe_load(_resolve_repo_path(robot["initial_positions_yaml"]).read_text(encoding="utf-8"))
    config["initial_q"] = [float(initial_payload[name]) for name in JOINT_NAMES]

    source_model = mujoco.MjModel.from_xml_string(resolved_urdf)
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".xml") as handle:
        mujoco.mj_saveLastXML(handle.name, source_model)
        canonical = Path(handle.name).read_text(encoding="utf-8")
    canonical_root = ET.fromstring(canonical)
    urdf_meshes = _referenced_meshes(resolved_urdf)
    surface_mesh = _resolve_repo_path(config["surface"]["mesh"])
    vendored_assets = _vendor_assets(canonical_root, output_dir, urdf_meshes, surface_mesh)
    canonical_portable = ET.tostring(canonical_root, encoding="unicode")

    denylist = config["legacy_denylist"]
    outputs: dict[str, dict[str, object]] = {}
    model_stats: dict[str, dict[str, object]] = {}
    for mode, filename in (("velocity", VELOCITY_MODEL), ("torque_surrogate", TORQUE_MODEL)):
        xml_text = patch_mjcf(canonical_portable, config=config, urdf_text=resolved_urdf, actuator_mode=mode)
        for token in denylist["tokens"]:
            if str(token) in xml_text:
                raise ValueError(f"generated MJCF contains denied legacy token: {token}")
        path = output_dir / filename
        path.write_text(xml_text + "\n", encoding="utf-8")
        model = mujoco.MjModel.from_xml_path(str(path))
        model_stats[mode] = _validate_model(model, mode=mode)
        outputs[mode] = binding(
            path,
            role=f"mujoco_{mode}_plant",
            claim_level=("geometry_provisional" if mode == "velocity" else "torque_physics_surrogate_not_direct_torque_reproduction"),
        )

    urdf_path = output_dir / "calibrated_ur10e.urdf"
    urdf_path.write_text(resolved_urdf, encoding="utf-8")
    source_bindings = [
        binding(inputs_path, role="digital_twin_input_manifest", claim_level="source"),
        binding(calibration, role="controller_calibration_yaml", claim_level="controller_calibrated"),
        binding(xacro_path, role="ur_description_entry_xacro", claim_level="package_pinned_by_hash"),
        binding(_resolve_repo_path(robot["initial_positions_yaml"]), role="initial_joint_state", claim_level="offline_initialization"),
        binding(surface_mesh, role="contact_surface_source", claim_level="geometry_only"),
    ]
    package_xml = package_root / "package.xml"
    if package_xml.is_file():
        source_bindings.append(binding(package_xml, role="ur_description_package_manifest", claim_level="package_pinned_by_hash"))
    for path in sorted((package_root / "urdf").rglob("*.xacro")):
        source_bindings.append(binding(path, role="ur_description_xacro_dependency", claim_level="package_pinned_by_hash"))

    manifest = {
        "schema": "ur10e_mujoco_model_bundle_v1",
        "generated_at": generated_at,
        "builder": binding(Path(__file__), role="builder", claim_level="source"),
        "environment": {
            "mujoco": mujoco.__version__,
            "pinocchio": pinocchio.__version__,
            "xacro": getattr(xacro, "__version__", "ros_humble_system"),
            "numpy": np.__version__,
        },
        "calibration_hash": calibration_hash,
        "generated_urdf": binding(urdf_path, role="calibrated_flattened_urdf", claim_level="controller_calibrated_robot_geometry"),
        "source_bindings": source_bindings,
        "vendored_assets": vendored_assets,
        "outputs": outputs,
        "model_stats": model_stats,
        "rates_hz": config["rates_hz"],
        "integer_schedule": {"physics_per_control": 4, "physics_per_dbil": 10},
        "active_variant": config["active_variant"],
        "active_tcp_offset_tool0_m": config["variants"][config["active_variant"]]["active_tcp_offset_tool0_m"],
        "command_jacobian_source": "calibrated_pinocchio_only",
        "mujoco_fk_jacobian_role": "independent_oracle_only_never_command_source",
        "velocity_plant_claim": "geometry_provisional offline physics",
        "torque_plant_claim": "torque-physics surrogate; not UR direct_torque reproduction",
        "claim_boundary": config["claim_boundary"],
        "blockers": config["blockers"],
        "safety_boundary": [
            "offline model generation only",
            "no controller upload",
            "no bridge start",
            "no TP Play",
            "no zero-FT",
            "no robot motion",
        ],
    }
    manifest_path = output_dir / "model_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, default=DEFAULT_INPUTS)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generated-at", default=datetime.now(timezone.utc).isoformat())
    args = parser.parse_args()
    result = build(
        inputs_path=args.inputs.resolve(),
        output_dir=args.output_dir.resolve(),
        generated_at=args.generated_at,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
