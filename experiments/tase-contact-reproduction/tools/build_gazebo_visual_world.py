#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
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


def _reference_rows(stage_id: str) -> list[runner.ReferencePoint]:
    return runner.build_reference_rows(stage_id)


def _retarget_surface_to_path(world: ET.Element, stage_id: str, rows: list[runner.ReferencePoint]) -> None:
    surface_name = SURFACE_MODELS_BY_STAGE[stage_id][0]
    surface = world.find(f"./model[@name='{surface_name}']")
    if surface is None or not rows:
        return

    xs = [row.x_m for row in rows]
    ys = [row.y_m for row in rows]
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
        x_m, y_m = row.x_m, row.y_m
        z_m = CONTACT_SURFACE_Z_M + 0.018 if matrix.STAGE_REGISTRY[stage_id].contact else row.z_m
        rgba = "1.0 0.85 0.0 1.0"
        radius = 0.006
        if index == 0:
            rgba = "0.0 1.0 0.0 1.0"
            radius = 0.010
        elif index == len(sampled) - 1:
            rgba = "1.0 0.0 0.0 1.0"
            radius = 0.010
        _sphere_link(model, f"path_{index:03d}", (float(x_m), float(y_m), z_m), radius, rgba)
    return len(sampled)


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
    marker_count = _add_reference_path(world, stage_id, rows)
    manifest = _build_manifest(
        world,
        stage_id=stage_id,
        base_world=base_world,
        output=output,
        rows=rows,
        marker_count=marker_count,
        removed_models=removed_models,
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
) -> dict[str, object]:
    path_bounds = _path_bounds(rows)
    surface = _surface_footprint(world, stage_id)
    return {
        "schema": "ur10e_gazebo_stage_visual_world_manifest_v1",
        "stage_id": stage_id,
        "base_world": str(base_world),
        "output_world": str(output),
        "kept_stage_visual_models": list(SURFACE_MODELS_BY_STAGE[stage_id]),
        "removed_stage_visual_models": sorted(removed_models),
        "surface_model": SURFACE_MODELS_BY_STAGE[stage_id][0],
        "reference_path_model": f"{stage_id}_reference_path_visual",
        "reference_row_count": len(rows),
        "reference_marker_count": marker_count,
        "path_bounds_xy_m": path_bounds,
        "surface": surface,
        "path_inside_surface_xy": _bounds_inside_surface(path_bounds, surface),
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
        "min_x_m": center_x - 0.5 * size_x,
        "max_x_m": center_x + 0.5 * size_x,
        "min_y_m": center_y - 0.5 * size_y,
        "max_y_m": center_y + 0.5 * size_y,
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
