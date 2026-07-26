import json
import math
from pathlib import Path

import FreeCAD as App
import Part
from FreeCAD import Vector


VERSION_DIR = Path("/Users/andyl/Documents/UR5e/eoat_print/v3")


PARAMS = {
    # UR5e flange interface
    "adapter_outer_diameter_mm": 80.0,
    "adapter_thickness_mm": 12.0,
    "robot_pattern_pcd_mm": 50.0,
    "robot_screw_clearance_mm": 6.6,
    "robot_socket_head_counterbore_mm": 11.0,
    "robot_socket_head_cbore_depth_mm": 6.0,
    "robot_center_recess_mm": 31.7,
    "robot_center_recess_depth_mm": 2.0,
    # EOAT body
    "body_outer_diameter_mm": 56.0,
    "body_height_mm": 36.0,
    "lower_collar_diameter_mm": 36.0,
    "lower_collar_height_mm": 16.0,
    "stem_diameter_mm": 20.0,
    "stem_length_mm": 44.0,
    # Fork and roller
    "fork_outer_width_mm": 24.0,
    "fork_depth_mm": 16.0,
    "fork_height_mm": 20.0,
    "fork_slot_width_mm": 13.0,
    "fork_slot_height_mm": 18.0,
    "axle_diameter_mm": 4.0,
    "axle_hole_diameter_mm": 4.1,
    "roller_bore_diameter_mm": 4.4,
    "roller_diameter_mm": 18.0,
    "roller_width_mm": 12.0,
    "roller_protrusion_below_fork_mm": 1.0,
    # Optional lightweight center bore
    "center_bore_mm": 6.0,
}


def polar_points(radius_mm: float, count: int, offset_deg: float = 0.0):
    points = []
    for idx in range(count):
        angle_deg = offset_deg + idx * 360.0 / count
        angle = math.radians(angle_deg)
        points.append((radius_mm * math.cos(angle), radius_mm * math.sin(angle)))
    return points


def make_cylinder_z(diameter_mm: float, height_mm: float, z0_mm: float):
    return Part.makeCylinder(diameter_mm / 2.0, height_mm, Vector(0, 0, z0_mm), Vector(0, 0, 1))


def make_cylinder_x(diameter_mm: float, length_mm: float, x0_mm: float, y_mm: float, z_mm: float):
    return Part.makeCylinder(diameter_mm / 2.0, length_mm, Vector(x0_mm, y_mm, z_mm), Vector(1, 0, 0))


def bbox_dict(shape):
    bb = shape.BoundBox
    return {
        "xmin": bb.XMin,
        "xmax": bb.XMax,
        "ymin": bb.YMin,
        "ymax": bb.YMax,
        "zmin": bb.ZMin,
        "zmax": bb.ZMax,
        "xlen": bb.XLength,
        "ylen": bb.YLength,
        "zlen": bb.ZLength,
    }


def build_adapter_disc(thickness_mm: float):
    p = PARAMS
    disc = make_cylinder_z(p["adapter_outer_diameter_mm"], thickness_mm, 0.0)

    center_recess = make_cylinder_z(
        p["robot_center_recess_mm"],
        p["robot_center_recess_depth_mm"] + 0.2,
        0.0,
    )
    disc = disc.cut(center_recess)

    center_bore = make_cylinder_z(p["center_bore_mm"], thickness_mm + 0.4, -0.2)
    disc = disc.cut(center_bore)

    robot_radius = p["robot_pattern_pcd_mm"] / 2.0
    for x, y in polar_points(robot_radius, 4, offset_deg=45.0):
        through = Part.makeCylinder(
            p["robot_screw_clearance_mm"] / 2.0,
            thickness_mm + 0.4,
            Vector(x, y, -0.2),
            Vector(0, 0, 1),
        )
        counterbore = Part.makeCylinder(
            p["robot_socket_head_counterbore_mm"] / 2.0,
            p["robot_socket_head_cbore_depth_mm"] + 0.2,
            Vector(x, y, thickness_mm - p["robot_socket_head_cbore_depth_mm"]),
            Vector(0, 0, 1),
        )
        disc = disc.cut(through).cut(counterbore)

    return disc


def build_body():
    p = PARAMS

    z_adapter = 0.0
    z_body = z_adapter + p["adapter_thickness_mm"]
    z_collar = z_body + p["body_height_mm"]
    z_stem = z_collar + p["lower_collar_height_mm"]
    z_fork = z_stem + p["stem_length_mm"]

    body = build_adapter_disc(p["adapter_thickness_mm"])
    body = body.fuse(make_cylinder_z(p["body_outer_diameter_mm"], p["body_height_mm"], z_body))
    body = body.fuse(make_cylinder_z(p["lower_collar_diameter_mm"], p["lower_collar_height_mm"], z_collar))
    body = body.fuse(make_cylinder_z(p["stem_diameter_mm"], p["stem_length_mm"], z_stem))

    fork = Part.makeBox(
        p["fork_outer_width_mm"],
        p["fork_depth_mm"],
        p["fork_height_mm"],
        Vector(-p["fork_outer_width_mm"] / 2.0, -p["fork_depth_mm"] / 2.0, z_fork),
    )
    body = body.fuse(fork)

    slot = Part.makeBox(
        p["fork_slot_width_mm"],
        p["fork_depth_mm"] + 0.4,
        p["fork_slot_height_mm"],
        Vector(-p["fork_slot_width_mm"] / 2.0, -(p["fork_depth_mm"] + 0.4) / 2.0, z_fork),
    )
    body = body.cut(slot)

    roller_radius = p["roller_diameter_mm"] / 2.0
    axle_z = z_fork + roller_radius + p["roller_protrusion_below_fork_mm"]
    axle_length = p["fork_outer_width_mm"] + 2.0
    axle_hole = make_cylinder_x(
        p["axle_hole_diameter_mm"],
        axle_length,
        -axle_length / 2.0,
        0.0,
        axle_z,
    )
    body = body.cut(axle_hole)

    # Keep the central bore only through the cylindrical trunk, not through the fork.
    body_bore = make_cylinder_z(p["center_bore_mm"], z_fork, 0.0)
    body = body.cut(body_bore)

    return body, axle_z, z_fork


def build_roller(axle_z_mm: float):
    p = PARAMS
    x0 = -p["roller_width_mm"] / 2.0
    roller = make_cylinder_x(p["roller_diameter_mm"], p["roller_width_mm"], x0, 0.0, axle_z_mm)
    bore = make_cylinder_x(
        p["roller_bore_diameter_mm"],
        p["roller_width_mm"] + 0.4,
        x0 - 0.2,
        0.0,
        axle_z_mm,
    )
    return roller.cut(bore)


def build_axle(axle_z_mm: float):
    p = PARAMS
    return make_cylinder_x(
        p["axle_diameter_mm"],
        p["fork_outer_width_mm"],
        -p["fork_outer_width_mm"] / 2.0,
        0.0,
        axle_z_mm,
    )


def build_fitcheck():
    return build_adapter_disc(4.0)


def save_shape(shape, stem: str):
    shape.exportStep(str(VERSION_DIR / f"{stem}.step"))
    shape.exportStl(str(VERSION_DIR / f"{stem}.stl"))


def main():
    VERSION_DIR.mkdir(parents=True, exist_ok=True)

    body, axle_z, z_fork = build_body()
    roller = build_roller(axle_z)
    axle = build_axle(axle_z)
    fitcheck = build_fitcheck()
    assembly = Part.makeCompound([body, roller, axle])
    onepiece = body.fuse(roller).fuse(axle)

    save_shape(body, "ur5e_roller_probe_v3_body")
    save_shape(roller, "ur5e_roller_probe_v3_roller")
    save_shape(axle, "ur5e_roller_probe_v3_axle")
    save_shape(fitcheck, "ur5e_roller_probe_v3_fitcheck")
    save_shape(assembly, "ur5e_roller_probe_v3_assembly")
    save_shape(onepiece, "ur5e_roller_probe_v3_onepiece")

    verification = {
        "version": "v3",
        "concept": "Thicker UR5e direct-mount column with bottom roller contact.",
        "parameters": PARAMS,
        "derived": {
            "fork_base_z_mm": z_fork,
            "roller_axle_z_mm": axle_z,
            "contact_point_from_flange_face_mm": axle_z - PARAMS["roller_diameter_mm"] / 2.0,
            "overall_tool_length_mm": onepiece.BoundBox.ZMax,
            "main_column_diameter_mm": PARAMS["stem_diameter_mm"],
            "roller_diameter_mm": PARAMS["roller_diameter_mm"],
        },
        "bounding_boxes": {
            "body": bbox_dict(body),
            "roller": bbox_dict(roller),
            "axle": bbox_dict(axle),
            "assembly": bbox_dict(assembly),
            "onepiece": bbox_dict(onepiece),
            "fitcheck": bbox_dict(fitcheck),
        },
        "assumptions_to_confirm": [
            "UR5e center locating recess diameter is still assumed as 31.7 mm, not yet measured on the bare flange.",
            "Roller and axle clearances are concept-level and should be tuned after first print.",
            "This v3 keeps the same 50-4-M6 interface logic used in earlier versions.",
        ],
    }
    (VERSION_DIR / "verification.json").write_text(json.dumps(verification, indent=2), encoding="utf-8")

    readme = f"""# UR5e roller probe concept v3

这个版本把 `v2` 的细探针改成了更粗的柱体，并把底部改成了滚轮式接触端。

## 当前判断

- 这是 `UR5e`（Universal Robots 5e）裸法兰直连概念版
- 重点变化是更粗的中心柱和底部滚轮
- 仍然不是 paper 同款尺寸复刻，而是高保真近似

## 输出文件

- `ur5e_roller_probe_v3_body.step / .stl`
- `ur5e_roller_probe_v3_roller.step / .stl`
- `ur5e_roller_probe_v3_axle.step / .stl`
- `ur5e_roller_probe_v3_fitcheck.step / .stl`
- `ur5e_roller_probe_v3_assembly.step / .stl`
- `ur5e_roller_probe_v3_onepiece.step / .stl`

## 主要尺寸

- 主体圆柱外径：{PARAMS["body_outer_diameter_mm"]:.1f} mm
- 下方粗柱直径：{PARAMS["stem_diameter_mm"]:.1f} mm
- 滚轮直径：{PARAMS["roller_diameter_mm"]:.1f} mm
- 滚轮宽度：{PARAMS["roller_width_mm"]:.1f} mm
- 接触点距法兰面的轴向距离：{verification["derived"]["contact_point_from_flange_face_mm"]:.1f} mm

## 说明

- `onepiece` 适合快速看比例或直接试打一体件
- 如果你真想让滚轮转起来，应优先看分件版 `body + roller + axle`
- 机器人侧接口仍沿用前一版的 `50-4-M6`
"""
    (VERSION_DIR / "README.md").write_text(readme, encoding="utf-8")

    print(VERSION_DIR / "ur5e_roller_probe_v3_onepiece.step")
    print(VERSION_DIR / "verification.json")


if __name__ == "__main__":
    main()
