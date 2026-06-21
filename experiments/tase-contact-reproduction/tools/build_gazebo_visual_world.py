#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[3]
SRC_PACKAGE = WORKSPACE / "src" / "ur10e_example_controllers"
if str(SRC_PACKAGE) not in sys.path:
    sys.path.insert(0, str(SRC_PACKAGE))

from ur10e_example_controllers import step56_simulation_matrix as matrix  # noqa: E402
from ur10e_example_controllers import ur10e_gazebo_matrix_runner as runner  # noqa: E402


SURFACE_MODELS_BY_STAGE = {
    "step5a": ("step5_contact_surface", "step5_safe_frame_origin_marker"),
    "step5b": ("step5_contact_surface", "step5_safe_frame_origin_marker"),
    "step5c": ("step5_contact_surface", "step5_safe_frame_origin_marker"),
    "step5d": ("step5_contact_surface", "step5_safe_frame_origin_marker"),
    "step6a": ("step6_contact_surface", "step6_safe_frame_origin_marker"),
    "step6b": ("step6_contact_surface", "step6_safe_frame_origin_marker"),
    "step7": ("step7_large_platform_contact_surface", "step7_large_platform_origin_marker"),
    "step8": ("step8_large_platform_contact_surface", "step8_large_platform_origin_marker"),
}
ALL_STAGE_VISUAL_MODELS = {
    "step5_contact_surface",
    "step5_safe_frame_origin_marker",
    "step6_contact_surface",
    "step6_safe_frame_origin_marker",
    "step7_large_platform_contact_surface",
    "step7_large_platform_origin_marker",
    "step8_large_platform_contact_surface",
    "step8_large_platform_origin_marker",
}
CONTACT_SURFACE_Z_M = 0.008044839
SURFACE_MARGIN_M = 0.04
SURFACE_AFFORDANCE_Z_OFFSET_M = 0.020
SURFACE_AFFORDANCE_THICKNESS_M = 0.010
CONTACT_TARGET_AFFORDANCE_Z_OFFSET_M = 0.070
SCRIPTED_CAMERA_PROFILES = {
    "context_overview": {
        "offset_xyz_m": (1.05, -1.20, 0.80),
        "horizontal_fov_rad": 1.05,
        "image_size": (1280, 900),
    },
    "interaction_view": {
        "offset_xyz_m": (0.70, -0.82, 0.52),
        "horizontal_fov_rad": 0.85,
        "image_size": (1280, 900),
    },
    "side_view": {
        "offset_xyz_m": (0.00, -1.05, 0.42),
        "horizontal_fov_rad": 0.82,
        "image_size": (1280, 900),
    },
    "close_detail": {
        "offset_xyz_m": (0.70, -0.82, 0.30),
        "target_offset_xyz_m": (0.0, 0.0, -0.095),
        "horizontal_fov_rad": 1.05,
        "image_size": (1280, 900),
    },
}
CONTACT_SYSTEM_PLUGIN_NAME = "gz::sim::systems::Contact"
CONTACT_SYSTEM_PLUGIN_FILENAME = "ignition-gazebo-contact-system"
CONTACT_SENSOR_UPDATE_RATE_HZ = 250
CONTACT_TOPIC_PREFIX = "/ur10e/contact/gazebo"
CONTACT_PAIR_LOGGING_CLAIM_TIER = "visual_only"


def _material(parent: ET.Element, rgba: str) -> None:
    material = ET.SubElement(parent, "material")
    ET.SubElement(material, "ambient").text = rgba
    ET.SubElement(material, "diffuse").text = rgba


def _sphere_link(parent: ET.Element, name: str, xyz: tuple[float, float, float], radius: float, rgba: str) -> None:
    link = ET.SubElement(parent, "link", {"name": name})
    ET.SubElement(link, "pose").text = f"{xyz[0]:.6f} {xyz[1]:.6f} {xyz[2]:.6f} 0 0 0"
    visual = ET.SubElement(link, "visual", {"name": "visual"})
    geometry = ET.SubElement(visual, "geometry")
    sphere = ET.SubElement(geometry, "sphere")
    ET.SubElement(sphere, "radius").text = f"{radius:.6f}"
    _material(visual, rgba)


def _box_link(
    parent: ET.Element,
    name: str,
    xyz: tuple[float, float, float],
    size_xyz: tuple[float, float, float],
    rgba: str,
) -> None:
    link = ET.SubElement(parent, "link", {"name": name})
    ET.SubElement(link, "pose").text = f"{xyz[0]:.6f} {xyz[1]:.6f} {xyz[2]:.6f} 0 0 0"
    visual = ET.SubElement(link, "visual", {"name": "visual"})
    geometry = ET.SubElement(visual, "geometry")
    box = ET.SubElement(geometry, "box")
    ET.SubElement(box, "size").text = f"{size_xyz[0]:.6f} {size_xyz[1]:.6f} {size_xyz[2]:.6f}"
    _material(visual, rgba)


def _cylinder_link(
    parent: ET.Element,
    name: str,
    xyz: tuple[float, float, float],
    radius: float,
    length: float,
    rgba: str,
    *,
    rpy: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> None:
    link = ET.SubElement(parent, "link", {"name": name})
    ET.SubElement(link, "pose").text = (
        f"{xyz[0]:.6f} {xyz[1]:.6f} {xyz[2]:.6f} "
        f"{rpy[0]:.9f} {rpy[1]:.9f} {rpy[2]:.9f}"
    )
    visual = ET.SubElement(link, "visual", {"name": "visual"})
    geometry = ET.SubElement(visual, "geometry")
    cylinder = ET.SubElement(geometry, "cylinder")
    ET.SubElement(cylinder, "radius").text = f"{radius:.6f}"
    ET.SubElement(cylinder, "length").text = f"{length:.6f}"
    _material(visual, rgba)


def _reference_rows(stage_id: str) -> list[runner.ReferencePoint]:
    return runner.build_reference_rows(stage_id)


def _retarget_surface_to_path(world: ET.Element, stage_id: str, rows: list[runner.ReferencePoint]) -> None:
    surface_name = SURFACE_MODELS_BY_STAGE[stage_id][0]
    surface = world.find(f"./model[@name='{surface_name}']")
    if surface is None or not rows:
        return

    world_points = [_visual_xyz_from_reference(row) for row in rows]
    xs = [point[0] for point in world_points]
    ys = [point[1] for point in world_points]
    center_x = 0.5 * (min(xs) + max(xs))
    center_y = 0.5 * (min(ys) + max(ys))
    size_x = max(max(xs) - min(xs) + SURFACE_MARGIN_M, 0.08)
    size_y = max(max(ys) - min(ys) + SURFACE_MARGIN_M, 0.08)

    pose = surface.find("pose")
    if pose is not None and pose.text:
        parts = pose.text.split()
        z = float(parts[2]) if len(parts) >= 3 else 0.0040224195
    else:
        z = 0.0040224195
        pose = ET.SubElement(surface, "pose")
    pose.text = f"{center_x:.15f} {center_y:.15f} {z:.10f} 0 0 0"

    for size in surface.findall(".//box/size"):
        parts = (size.text or "0 0 0.008044839").split()
        z_size = float(parts[2]) if len(parts) >= 3 else 0.008044839
        size.text = f"{size_x:.6f} {size_y:.6f} {z_size:.9f}"


def _add_reference_path(world: ET.Element, stage_id: str, rows: list[runner.ReferencePoint]) -> int:
    if not rows:
        return 0

    model = ET.SubElement(world, "model", {"name": f"{stage_id}_reference_path_visual"})
    ET.SubElement(model, "static").text = "true"
    stride = max(1, len(rows) // 40)
    sampled = list(rows[::stride])
    if sampled[-1] is not rows[-1]:
        sampled.append(rows[-1])

    for index, row in enumerate(sampled):
        x_m, y_m, _ = _visual_xyz_from_reference(row)
        z_m = CONTACT_SURFACE_Z_M + 0.060 if matrix.STAGE_REGISTRY[stage_id].contact else row.z_m
        rgba = "1.0 0.85 0.0 1.0"
        radius = 0.010 if matrix.STAGE_REGISTRY[stage_id].contact else 0.006
        if index == 0:
            rgba = "0.0 1.0 0.0 1.0"
            radius = 0.014
        elif index == len(sampled) - 1:
            rgba = "1.0 0.0 0.0 1.0"
            radius = 0.014
        _sphere_link(model, f"path_{index:03d}", (float(x_m), float(y_m), z_m), radius, rgba)
    return len(sampled)


def _add_surface_viewer_affordances(
    world: ET.Element,
    stage_id: str,
    rows: list[runner.ReferencePoint],
) -> dict[str, object]:
    surface = _surface_footprint(world, stage_id)
    if not rows or surface["center_x_m"] is None or surface["top_z_m"] is None:
        return {"model": None, "added": False}

    model_name = f"{stage_id}_surface_viewer_affordance"
    model = ET.SubElement(world, "model", {"name": model_name})
    ET.SubElement(model, "static").text = "true"
    ET.SubElement(model, "pose").text = "0 0 0 0 0 0"

    center_x = float(surface["center_x_m"])
    center_y = float(surface["center_y_m"])
    size_x = float(surface["size_x_m"])
    size_y = float(surface["size_y_m"])
    top_z = float(surface["top_z_m"])
    z = top_z + SURFACE_AFFORDANCE_Z_OFFSET_M
    thickness = SURFACE_AFFORDANCE_THICKNESS_M
    height = 0.010
    rim_color = "1.0 0.85 0.0 1.0"
    target_color = "1.0 0.0 1.0 1.0"
    path_color = "0.0 1.0 1.0 1.0"

    _box_link(model, "surface_front_rim", (center_x, center_y - 0.5 * size_y, z), (size_x, thickness, height), rim_color)
    _box_link(model, "surface_back_rim", (center_x, center_y + 0.5 * size_y, z), (size_x, thickness, height), rim_color)
    _box_link(model, "surface_left_rim", (center_x - 0.5 * size_x, center_y, z), (thickness, size_y, height), rim_color)
    _box_link(model, "surface_right_rim", (center_x + 0.5 * size_x, center_y, z), (thickness, size_y, height), rim_color)
    post_z = top_z + 0.055
    post_length = 0.110
    for name, x_m, y_m, color in (
        ("surface_front_left_witness_post", center_x - 0.5 * size_x, center_y - 0.5 * size_y, "1.0 0.0 1.0 1.0"),
        ("surface_front_right_witness_post", center_x + 0.5 * size_x, center_y - 0.5 * size_y, "0.0 1.0 1.0 1.0"),
        ("surface_back_left_witness_post", center_x - 0.5 * size_x, center_y + 0.5 * size_y, "1.0 1.0 1.0 1.0"),
        ("surface_back_right_witness_post", center_x + 0.5 * size_x, center_y + 0.5 * size_y, "1.0 0.85 0.0 1.0"),
    ):
        _cylinder_link(model, name, (x_m, y_m, post_z), 0.0055, post_length, color)

    target_xyz = _contact_target_pose_world(surface, rows[-1]) if matrix.STAGE_REGISTRY[stage_id].contact else None
    if target_xyz is not None:
        tx = float(target_xyz["x_m"])
        ty = float(target_xyz["y_m"])
        tz = top_z + CONTACT_TARGET_AFFORDANCE_Z_OFFSET_M
        _sphere_link(model, "contact_target_center_marker", (tx, ty, tz), 0.012, target_color)
        _cylinder_link(
            model,
            "contact_target_cross_x",
            (tx, ty, tz),
            0.004,
            min(max(size_x * 0.85, 0.055), 0.120),
            target_color,
            rpy=(0.0, math.pi / 2.0, 0.0),
        )
        _cylinder_link(
            model,
            "contact_target_cross_y",
            (tx, ty, tz),
            0.004,
            min(max(size_y * 0.65, 0.055), 0.120),
            path_color,
            rpy=(math.pi / 2.0, 0.0, 0.0),
        )
        _cylinder_link(model, "contact_target_vertical_witness", (tx, ty, top_z + 0.065), 0.0045, 0.130, "1.0 1.0 1.0 1.0")

    return {
        "model": model_name,
        "added": True,
        "policy": "non_colliding_viewer_affordance_surface_outline_and_contact_target_marker",
        "surface_rim_z_m": z,
        "contact_target_marker_z_m": top_z + CONTACT_TARGET_AFFORDANCE_Z_OFFSET_M
        if target_xyz is not None
        else None,
    }


def _add_scripted_cameras(
    world: ET.Element,
    stage_id: str,
    rows: list[runner.ReferencePoint],
) -> dict[str, dict[str, object]]:
    if not rows:
        return {}
    _ensure_sensors_system(world)
    surface = _surface_footprint(world, stage_id)
    base_target = _scripted_camera_target(stage_id, rows, surface)
    cameras: dict[str, dict[str, object]] = {}
    for view, profile in SCRIPTED_CAMERA_PROFILES.items():
        offset = profile["offset_xyz_m"]
        target_offset = profile.get("target_offset_xyz_m", (0.0, 0.0, 0.0))
        target = (
            base_target[0] + float(target_offset[0]),
            base_target[1] + float(target_offset[1]),
            base_target[2] + float(target_offset[2]),
        )
        camera_xyz = (
            target[0] + float(offset[0]),
            target[1] + float(offset[1]),
            target[2] + float(offset[2]),
        )
        pose = _look_at_pose(camera_xyz, target)
        topic = f"/ur10e_visual_audit/{stage_id}/{view}/image"
        width, height = profile["image_size"]
        model_name = f"{stage_id}_{view}_scripted_camera"
        _camera_model(
            world,
            name=model_name,
            pose=pose,
            topic=topic,
            width=int(width),
            height=int(height),
            horizontal_fov_rad=float(profile["horizontal_fov_rad"]),
        )
        cameras[view] = {
            "model": model_name,
            "topic": topic,
            "frame": runner.GAZEBO_WORLD_FRAME,
            "pose_xyz_rpy": list(pose),
            "target_xyz_m": list(target),
            "base_target_xyz_m": list(base_target),
            "offset_xyz_m": list(offset),
            "target_offset_xyz_m": list(target_offset),
            "horizontal_fov_rad": float(profile["horizontal_fov_rad"]),
            "image_width": int(width),
            "image_height": int(height),
            "capture_policy": "scripted_gazebo_camera_clean_no_gui_panels_observer_review_still_required",
        }
    return cameras


def _ensure_sensors_system(world: ET.Element) -> None:
    for plugin in world.findall("plugin"):
        if plugin.get("name") == "gz::sim::systems::Sensors":
            return
    plugin = ET.SubElement(world, "plugin", {"filename": "gz-sim-sensors-system", "name": "gz::sim::systems::Sensors"})
    ET.SubElement(plugin, "render_engine").text = "ogre2"


def _ensure_contact_system(world: ET.Element) -> None:
    for plugin in world.findall("plugin"):
        if plugin.get("name") == CONTACT_SYSTEM_PLUGIN_NAME:
            return
    ET.SubElement(
        world,
        "plugin",
        {"filename": CONTACT_SYSTEM_PLUGIN_FILENAME, "name": CONTACT_SYSTEM_PLUGIN_NAME},
    )


def _add_contact_pair_logging(world: ET.Element, stage_id: str) -> dict[str, object]:
    if not matrix.STAGE_REGISTRY[stage_id].contact:
        return {
            "enabled": False,
            "claim_tier": CONTACT_PAIR_LOGGING_CLAIM_TIER,
            "contact_pair_log_evidence": False,
            "force_contact_physics_proven": False,
            "status": "disabled_for_non_contact_stage",
            "topic": None,
        }

    surface_model = SURFACE_MODELS_BY_STAGE[stage_id][0]
    surface = world.find(f"./model[@name='{surface_model}']")
    surface_link = surface.find("./link[@name='surface']") if surface is not None else None
    if surface_link is None:
        return {
            "enabled": False,
            "claim_tier": CONTACT_PAIR_LOGGING_CLAIM_TIER,
            "contact_pair_log_evidence": False,
            "force_contact_physics_proven": False,
            "status": "blocked_surface_link_missing",
            "surface_model": surface_model,
            "surface_link": "surface",
            "topic": None,
        }

    _ensure_contact_system(world)
    sensor_name = f"{stage_id}_surface_contact_sensor"
    for sensor in list(surface_link.findall(f"./sensor[@name='{sensor_name}']")):
        surface_link.remove(sensor)
    topic = f"{CONTACT_TOPIC_PREFIX}/{stage_id}/contacts"
    sensor = ET.SubElement(surface_link, "sensor", {"name": sensor_name, "type": "contact"})
    contact = ET.SubElement(sensor, "contact")
    ET.SubElement(contact, "collision").text = "collision"
    ET.SubElement(contact, "topic").text = topic
    ET.SubElement(sensor, "always_on").text = "1"
    ET.SubElement(sensor, "update_rate").text = str(CONTACT_SENSOR_UPDATE_RATE_HZ)
    return {
        "enabled": True,
        "claim_tier": CONTACT_PAIR_LOGGING_CLAIM_TIER,
        "contact_pair_log_evidence": False,
        "force_contact_physics_proven": False,
        "status": "instrumented_not_captured",
        "system_plugin": CONTACT_SYSTEM_PLUGIN_NAME,
        "system_plugin_filename": CONTACT_SYSTEM_PLUGIN_FILENAME,
        "surface_model": surface_model,
        "surface_link": "surface",
        "collision_name": "collision",
        "sensor_name": sensor_name,
        "topic": topic,
        "message_type": "ignition.msgs.Contacts",
        "future_artifact_schema": "ur10e_gazebo_contact_pair_log_v1",
        "claim_boundary": "instrumentation_only_no_contact_pair_log_or_wrench_correlation_captured",
    }


def _scripted_camera_target(
    stage_id: str,
    rows: list[runner.ReferencePoint],
    surface: dict[str, float | None],
) -> tuple[float, float, float]:
    final_xyz = _visual_xyz_from_reference(rows[-1])
    top_z = surface.get("top_z_m")
    if matrix.STAGE_REGISTRY[stage_id].contact and top_z is not None:
        target_z = float(top_z) + 0.13
    else:
        target_z = float(final_xyz[2]) + 0.10
    return float(final_xyz[0]), float(final_xyz[1]), target_z


def _look_at_pose(
    camera_xyz: tuple[float, float, float],
    target_xyz: tuple[float, float, float],
) -> tuple[float, float, float, float, float, float]:
    dx = target_xyz[0] - camera_xyz[0]
    dy = target_xyz[1] - camera_xyz[1]
    dz = target_xyz[2] - camera_xyz[2]
    yaw = math.atan2(dy, dx)
    pitch = math.atan2(-dz, math.hypot(dx, dy))
    return camera_xyz[0], camera_xyz[1], camera_xyz[2], 0.0, pitch, yaw


def _camera_model(
    world: ET.Element,
    *,
    name: str,
    pose: tuple[float, float, float, float, float, float],
    topic: str,
    width: int,
    height: int,
    horizontal_fov_rad: float,
) -> None:
    model = ET.SubElement(world, "model", {"name": name})
    ET.SubElement(model, "static").text = "true"
    ET.SubElement(model, "pose").text = " ".join(f"{value:.9f}" for value in pose)
    link = ET.SubElement(model, "link", {"name": "camera_link"})
    sensor = ET.SubElement(link, "sensor", {"name": "camera", "type": "camera"})
    ET.SubElement(sensor, "always_on").text = "1"
    ET.SubElement(sensor, "update_rate").text = "10"
    ET.SubElement(sensor, "topic").text = topic
    camera = ET.SubElement(sensor, "camera")
    ET.SubElement(camera, "horizontal_fov").text = f"{horizontal_fov_rad:.9f}"
    image = ET.SubElement(camera, "image")
    ET.SubElement(image, "width").text = str(width)
    ET.SubElement(image, "height").text = str(height)
    ET.SubElement(image, "format").text = "R8G8B8"
    clip = ET.SubElement(camera, "clip")
    ET.SubElement(clip, "near").text = "0.02"
    ET.SubElement(clip, "far").text = "8.0"


def build_visual_world(stage_id: str, base_world: Path, output: Path) -> Path:
    if stage_id not in SURFACE_MODELS_BY_STAGE:
        raise SystemExit(f"unknown stage {stage_id!r}")

    tree = ET.parse(base_world)
    root = tree.getroot()
    world = root.find("world")
    if world is None:
        raise SystemExit(f"world element not found in {base_world}")

    keep_models = set(SURFACE_MODELS_BY_STAGE[stage_id])
    removed_models: list[str] = []
    for model in list(world.findall("model")):
        name = model.get("name")
        if name in ALL_STAGE_VISUAL_MODELS and name not in keep_models:
            world.remove(model)
            removed_models.append(name)

    rows = _reference_rows(stage_id)
    _retarget_surface_to_path(world, stage_id, rows)
    surface_viewer_affordance = _add_surface_viewer_affordances(world, stage_id, rows)
    contact_pair_logging = _add_contact_pair_logging(world, stage_id)
    marker_count = _add_reference_path(world, stage_id, rows)
    scripted_cameras = _add_scripted_cameras(world, stage_id, rows)
    manifest = _build_manifest(
        world,
        stage_id=stage_id,
        base_world=base_world,
        output=output,
        rows=rows,
        marker_count=marker_count,
        removed_models=removed_models,
        scripted_cameras=scripted_cameras,
        surface_viewer_affordance=surface_viewer_affordance,
        contact_pair_logging=contact_pair_logging,
    )
    ET.indent(tree, space="  ")
    output.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output, encoding="utf-8", xml_declaration=True)
    output.with_suffix(".manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output


def _build_manifest(
    world: ET.Element,
    *,
    stage_id: str,
    base_world: Path,
    output: Path,
    rows: list[runner.ReferencePoint],
    marker_count: int,
    removed_models: list[str],
    scripted_cameras: dict[str, dict[str, object]],
    surface_viewer_affordance: dict[str, object],
    contact_pair_logging: dict[str, object],
) -> dict[str, object]:
    path_bounds_base = _path_bounds(rows)
    path_bounds_world = _path_bounds_world(rows)
    surface = _surface_footprint(world, stage_id)
    first_reference_pose = _reference_pose_payload(rows[0]) if rows else None
    final_reference_pose = _reference_pose_payload(rows[-1]) if rows else None
    first_visual_pose = _visual_pose_payload(rows[0]) if rows else None
    final_visual_pose = _visual_pose_payload(rows[-1]) if rows else None
    contact_target_pose_base = (
        _contact_target_pose_base(surface, rows[-1]) if rows and matrix.STAGE_REGISTRY[stage_id].contact else None
    )
    contact_target_pose_world = (
        _contact_target_pose_world(surface, rows[-1]) if rows and matrix.STAGE_REGISTRY[stage_id].contact else None
    )
    return {
        "schema": "ur10e_gazebo_stage_visual_world_manifest_v2",
        "stage_id": stage_id,
        "base_world": str(base_world),
        "output_world": str(output),
        "kept_stage_visual_models": list(SURFACE_MODELS_BY_STAGE[stage_id]),
        "removed_stage_visual_models": sorted(removed_models),
        "surface_model": SURFACE_MODELS_BY_STAGE[stage_id][0],
        "reference_path_model": f"{stage_id}_reference_path_visual",
        "reference_row_count": len(rows),
        "reference_marker_count": marker_count,
        "scripted_cameras": scripted_cameras,
        "surface_viewer_affordance": surface_viewer_affordance,
        "contact_pair_logging": contact_pair_logging,
        "path_bounds_base_xy_m": path_bounds_base,
        "path_bounds_xy_m": path_bounds_world,
        "surface_frame": runner.GAZEBO_WORLD_FRAME,
        "active_tcp_reference_frame": runner.ACTIVE_TCP_FRAME,
        "tool_frame": runner.TOOL0_FRAME,
        "base_to_gazebo_world_rpy": list(runner.BASE_TO_GAZEBO_WORLD_RPY),
        "active_tcp_offset_tool0_m": list(runner.ACTIVE_TCP_OFFSET_TOOL0_M),
        "first_reference_pose_base": first_reference_pose,
        "final_reference_pose_base": final_reference_pose,
        "first_visual_pose_world": first_visual_pose,
        "final_visual_pose_world": final_visual_pose,
        "contact_target_pose_base": contact_target_pose_base,
        "contact_target_pose_world": contact_target_pose_world,
        "surface": surface,
        "surface_mesh_visual": _surface_mesh_visual(world, stage_id),
        "path_inside_surface_xy": _bounds_inside_surface(path_bounds_world, surface),
        "surface_tcp_sanity": _surface_tcp_sanity(surface, rows, contact_target_pose_world),
        "contact_stage": bool(matrix.STAGE_REGISTRY[stage_id].contact),
        "force_loop_expected": stage_id in runner.CONTACT_STAGE_IDS,
    }


def _path_bounds(rows: list[runner.ReferencePoint]) -> dict[str, float | None]:
    if not rows:
        return {"min_x_m": None, "max_x_m": None, "min_y_m": None, "max_y_m": None}
    xs = [float(row.x_m) for row in rows]
    ys = [float(row.y_m) for row in rows]
    return {
        "min_x_m": min(xs),
        "max_x_m": max(xs),
        "min_y_m": min(ys),
        "max_y_m": max(ys),
    }


def _path_bounds_world(rows: list[runner.ReferencePoint]) -> dict[str, float | None]:
    if not rows:
        return {"min_x_m": None, "max_x_m": None, "min_y_m": None, "max_y_m": None}
    points = [_visual_xyz_from_reference(row) for row in rows]
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return {
        "min_x_m": min(xs),
        "max_x_m": max(xs),
        "min_y_m": min(ys),
        "max_y_m": max(ys),
    }


def _surface_footprint(world: ET.Element, stage_id: str) -> dict[str, float | None]:
    surface_name = SURFACE_MODELS_BY_STAGE[stage_id][0]
    surface = world.find(f"./model[@name='{surface_name}']")
    if surface is None:
        return {
            "center_x_m": None,
            "center_y_m": None,
            "z_m": None,
            "size_x_m": None,
            "size_y_m": None,
            "size_z_m": None,
            "top_z_m": None,
            "min_x_m": None,
            "max_x_m": None,
            "min_y_m": None,
            "max_y_m": None,
        }
    pose_values = [float(value) for value in (surface.findtext("pose") or "0 0 0 0 0 0").split()]
    size_values = [
        float(value)
        for value in (surface.findtext("./link/collision/geometry/box/size") or "0 0 0").split()
    ]
    center_x, center_y = pose_values[0], pose_values[1]
    size_x, size_y = size_values[0], size_values[1]
    return {
        "center_x_m": center_x,
        "center_y_m": center_y,
        "z_m": pose_values[2],
        "size_x_m": size_x,
        "size_y_m": size_y,
        "size_z_m": size_values[2],
        "top_z_m": pose_values[2] + 0.5 * size_values[2],
        "min_x_m": center_x - 0.5 * size_x,
        "max_x_m": center_x + 0.5 * size_x,
        "min_y_m": center_y - 0.5 * size_y,
        "max_y_m": center_y + 0.5 * size_y,
    }


def _surface_mesh_visual(world: ET.Element, stage_id: str) -> dict[str, object]:
    surface_name = SURFACE_MODELS_BY_STAGE[stage_id][0]
    surface = world.find(f"./model[@name='{surface_name}']")
    visual = (
        surface.find(f".//visual[@name='{runner.CONTACT_SURFACE_REAL_MESH_VISUAL_NAME}']")
        if surface is not None
        else None
    )
    mesh = visual.find("./geometry/mesh") if visual is not None else None
    uri = mesh.findtext("uri") if mesh is not None else None
    scale = mesh.findtext("scale") if mesh is not None else None
    pose = visual.findtext("pose") if visual is not None else None
    return {
        "primary_visual_name": runner.CONTACT_SURFACE_REAL_MESH_VISUAL_NAME,
        "primary_visual_uses_real_mesh": bool(uri == runner.CONTACT_SURFACE_REAL_MESH_URI),
        "mesh_uri": uri,
        "expected_mesh_uri": runner.CONTACT_SURFACE_REAL_MESH_URI,
        "mesh_source_asset": runner.CONTACT_SURFACE_REAL_MESH_SOURCE_ASSET,
        "coupon_mesh_source_asset_available": runner.CONTACT_SURFACE_COUPON_MESH_SOURCE_ASSET,
        "scale": scale,
        "expected_scale": " ".join(f"{value:.12g}" for value in runner.CONTACT_SURFACE_REAL_MESH_SCALE),
        "pose_xyz_rpy": pose,
        "expected_pose_xyz_rpy": " ".join(f"{value:.12g}" for value in runner.CONTACT_SURFACE_REAL_MESH_VISUAL_POSE),
        "raw_bbox_mm": runner.CONTACT_SURFACE_REAL_MESH_RAW_BBOX_MM,
        "oriented_bbox_m": runner.CONTACT_SURFACE_REAL_MESH_ORIENTED_BBOX_M,
        "collision_primitive_remains": True,
        "collision_primitive_policy": "box collision retained only as simplified collision/contact sensor surface",
    }


def _bounds_inside_surface(path_bounds: dict[str, float | None], surface: dict[str, float | None]) -> bool:
    required = ("min_x_m", "max_x_m", "min_y_m", "max_y_m")
    if any(path_bounds[key] is None or surface[key] is None for key in required):
        return False
    return bool(
        surface["min_x_m"] <= path_bounds["min_x_m"]
        and surface["max_x_m"] >= path_bounds["max_x_m"]
        and surface["min_y_m"] <= path_bounds["min_y_m"]
        and surface["max_y_m"] >= path_bounds["max_y_m"]
    )


def _reference_pose_payload(row: runner.ReferencePoint) -> dict[str, float | str]:
    return {
        "frame": runner.ACTIVE_TCP_FRAME,
        "x_m": float(row.x_m),
        "y_m": float(row.y_m),
        "z_m": float(row.z_m),
        "t_s": float(row.t_s),
        "segment": row.segment,
    }


def _visual_xyz_from_reference(row: runner.ReferencePoint) -> tuple[float, float, float]:
    return runner.gazebo_world_xyz_from_base_xyz((row.x_m, row.y_m, row.z_m))


def _visual_pose_payload(row: runner.ReferencePoint) -> dict[str, float | str]:
    x_m, y_m, z_m = _visual_xyz_from_reference(row)
    return {
        "frame": runner.GAZEBO_WORLD_FRAME,
        "source_frame": runner.ACTIVE_TCP_FRAME,
        "x_m": x_m,
        "y_m": y_m,
        "z_m": z_m,
        "t_s": float(row.t_s),
        "segment": row.segment,
    }


def _contact_target_pose_base(
    surface: dict[str, float | None],
    final_reference: runner.ReferencePoint,
) -> dict[str, float | str] | None:
    top_z = surface.get("top_z_m")
    if top_z is None:
        return None
    return {
        "frame": runner.ACTIVE_TCP_FRAME,
        "x_m": float(final_reference.x_m),
        "y_m": float(final_reference.y_m),
        "z_m": float(top_z),
        "surface_top_z_m": float(top_z),
    }


def _contact_target_pose_world(
    surface: dict[str, float | None],
    final_reference: runner.ReferencePoint,
) -> dict[str, float | str] | None:
    top_z = surface.get("top_z_m")
    if top_z is None:
        return None
    x_m, y_m, _ = _visual_xyz_from_reference(final_reference)
    return {
        "frame": runner.GAZEBO_WORLD_FRAME,
        "source_frame": runner.ACTIVE_TCP_FRAME,
        "x_m": x_m,
        "y_m": y_m,
        "z_m": float(top_z),
        "surface_top_z_m": float(top_z),
    }


def _point_inside_surface(x_m: float, y_m: float, surface: dict[str, float | None]) -> bool:
    required = ("min_x_m", "max_x_m", "min_y_m", "max_y_m")
    if any(surface[key] is None for key in required):
        return False
    return bool(
        surface["min_x_m"] <= x_m <= surface["max_x_m"]
        and surface["min_y_m"] <= y_m <= surface["max_y_m"]
    )


def _surface_tcp_sanity(
    surface: dict[str, float | None],
    rows: list[runner.ReferencePoint],
    contact_target_pose_world: dict[str, float | str] | None,
) -> dict[str, object]:
    top_z = surface.get("top_z_m")
    final_reference = rows[-1] if rows else None
    final_world = _visual_xyz_from_reference(final_reference) if final_reference is not None else None
    final_inside = (
        _point_inside_surface(float(final_world[0]), float(final_world[1]), surface)
        if final_world is not None
        else False
    )
    contact_inside = (
        _point_inside_surface(float(contact_target_pose_world["x_m"]), float(contact_target_pose_world["y_m"]), surface)
        if contact_target_pose_world is not None
        else None
    )
    return {
        "same_frame": runner.GAZEBO_WORLD_FRAME,
        "source_frame": runner.ACTIVE_TCP_FRAME,
        "final_reference_xy_inside_surface": final_inside,
        "final_visual_xy_inside_surface": final_inside,
        "contact_target_xy_inside_surface": contact_inside,
        "contact_target_world_xy_inside_surface": contact_inside,
        "reference_final_z_minus_surface_top_m": float(final_reference.z_m - top_z)
        if final_reference is not None and top_z is not None
        else None,
        "contact_target_z_minus_surface_top_m": float(contact_target_pose_world["z_m"] - top_z)
        if contact_target_pose_world is not None and top_z is not None
        else None,
        "reaction_normal_base": [0.0, 0.0, 1.0],
        "approach_normal_base": [0.0, 0.0, -1.0],
        "approach_dot_reaction": -1.0,
        "force_frame_contract": "reaction_normal_for_load_approach_normal_for_posture_and_press",
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a stage-specific Gazebo visual-audit world.")
    parser.add_argument("--stage", required=True, choices=sorted(SURFACE_MODELS_BY_STAGE))
    parser.add_argument(
        "--base-world",
        type=Path,
        default=WORKSPACE / "src" / "ur10e_example_controllers" / "worlds" / "step5_table_world.sdf",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(build_visual_world(args.stage, args.base_world, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
