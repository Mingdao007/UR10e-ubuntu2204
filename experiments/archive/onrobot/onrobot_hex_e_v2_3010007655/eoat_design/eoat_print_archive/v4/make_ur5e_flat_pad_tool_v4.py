import json
import math
from pathlib import Path

import Part
from FreeCAD import Vector


VERSION_DIR = Path("/Users/andyl/Documents/UR5e/eoat_print/v4")


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
    # Paper-like body
    "upper_body_diameter_mm": 40.0,
    "upper_body_height_mm": 50.0,
    "lower_body_diameter_mm": 30.0,
    "lower_body_height_mm": 26.0,
    "pad_mount_diameter_mm": 18.0,
    "pad_mount_height_mm": 4.0,
    # Flat contact pad
    "pad_top_diameter_mm": 24.0,
    "pad_bottom_diameter_mm": 30.0,
    "pad_height_mm": 8.0,
    "pad_locating_recess_diameter_mm": 18.4,
    "pad_locating_recess_depth_mm": 3.2,
    # Lightweight center bore
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


def make_cone_z(bottom_diameter_mm: float, top_diameter_mm: float, height_mm: float, z0_mm: float):
    return Part.makeCone(
        bottom_diameter_mm / 2.0,
        top_diameter_mm / 2.0,
        height_mm,
        Vector(0, 0, z0_mm),
        Vector(0, 0, 1),
    )


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


def build_adapter_disc(thickness_mm: float, include_counterbores: bool = True):
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
        disc = disc.cut(through)
        if include_counterbores:
            counterbore_depth = min(p["robot_socket_head_cbore_depth_mm"], thickness_mm)
            counterbore = Part.makeCylinder(
                p["robot_socket_head_counterbore_mm"] / 2.0,
                counterbore_depth + 0.2,
                Vector(x, y, thickness_mm - counterbore_depth),
                Vector(0, 0, 1),
            )
            disc = disc.cut(counterbore)

    return disc


def build_body():
    p = PARAMS
    z_adapter = 0.0
    z_upper = z_adapter + p["adapter_thickness_mm"]
    z_lower = z_upper + p["upper_body_height_mm"]
    z_mount = z_lower + p["lower_body_height_mm"]

    body = build_adapter_disc(p["adapter_thickness_mm"])
    body = body.fuse(make_cylinder_z(p["upper_body_diameter_mm"], p["upper_body_height_mm"], z_upper))
    body = body.fuse(make_cylinder_z(p["lower_body_diameter_mm"], p["lower_body_height_mm"], z_lower))
    body = body.fuse(make_cylinder_z(p["pad_mount_diameter_mm"], p["pad_mount_height_mm"], z_mount))

    # Run a lightweight center bore only through the rigid trunk.
    bore = make_cylinder_z(p["center_bore_mm"], z_mount + p["pad_mount_height_mm"], 0.0)
    body = body.cut(bore)

    return body, z_mount


def build_contact_pad():
    p = PARAMS
    pad = make_cone_z(
        p["pad_bottom_diameter_mm"],
        p["pad_top_diameter_mm"],
        p["pad_height_mm"],
        0.0,
    )

    locating_recess = make_cylinder_z(
        p["pad_locating_recess_diameter_mm"],
        p["pad_locating_recess_depth_mm"] + 0.2,
        p["pad_height_mm"] - p["pad_locating_recess_depth_mm"],
    )
    pad = pad.cut(locating_recess)
    return pad


def build_fitcheck():
    return build_adapter_disc(4.0, include_counterbores=False)


def save_shape(shape, stem: str):
    shape.exportStep(str(VERSION_DIR / f"{stem}.step"))
    shape.exportStl(str(VERSION_DIR / f"{stem}.stl"))


def main():
    VERSION_DIR.mkdir(parents=True, exist_ok=True)

    body, z_mount = build_body()
    pad = build_contact_pad()
    pad = pad.translate(Vector(0, 0, z_mount))
    fitcheck = build_fitcheck()
    assembly = Part.makeCompound([body, pad])
    onepiece = body.fuse(pad)

    save_shape(body, "ur5e_flat_pad_tool_v4_body")
    save_shape(pad, "ur5e_flat_pad_tool_v4_pad")
    save_shape(fitcheck, "ur5e_flat_pad_tool_v4_fitcheck")
    save_shape(assembly, "ur5e_flat_pad_tool_v4_assembly")
    save_shape(onepiece, "ur5e_flat_pad_tool_v4_onepiece")

    contact_face_z = onepiece.BoundBox.ZMax
    verification = {
        "version": "v4",
        "concept": "Paper-like direct-mount UR5e flat-pad contact tool.",
        "parameters": PARAMS,
        "derived": {
            "upper_body_diameter_mm": PARAMS["upper_body_diameter_mm"],
            "lower_body_diameter_mm": PARAMS["lower_body_diameter_mm"],
            "contact_pad_bottom_diameter_mm": PARAMS["pad_bottom_diameter_mm"],
            "contact_pad_height_mm": PARAMS["pad_height_mm"],
            "contact_face_from_flange_face_mm": contact_face_z,
            "overall_tool_length_mm": onepiece.BoundBox.ZLength,
        },
        "bounding_boxes": {
            "body": bbox_dict(body),
            "pad": bbox_dict(pad),
            "assembly": bbox_dict(assembly),
            "onepiece": bbox_dict(onepiece),
            "fitcheck": bbox_dict(fitcheck),
        },
        "assumptions_to_confirm": [
            "This follows the paper-like flat contact appearance, not an exact published mechanical drawing.",
            "UR5e center locating recess diameter is still assumed as 31.7 mm and should be checked on the bare flange.",
            "If the real contact material should be foam, rubber, or abrasive pad, this printed pad is only the carrier geometry.",
        ],
    }
    (VERSION_DIR / "verification.json").write_text(json.dumps(verification, indent=2), encoding="utf-8")

    readme = f"""# UR5e flat-pad tool v4

这个版本不再走滚轮路线，而是按论文图和你刚发的参考图，收成更像原文的粗直筒 + 平底圆形接触垫。

## 当前判断

- 这是更接近 paper 风格的 `UR5e`（Universal Robots 5e）直连概念版
- 底部接触件是平底圆形垫，不是滚轮
- 如果以后需要真实软垫、海绵垫或打磨片，可以把当前 `pad` 当成载体几何继续改

## 输出文件

- `ur5e_flat_pad_tool_v4_body.step / .stl`
- `ur5e_flat_pad_tool_v4_pad.step / .stl`
- `ur5e_flat_pad_tool_v4_fitcheck.step / .stl`
- `ur5e_flat_pad_tool_v4_assembly.step / .stl`
- `ur5e_flat_pad_tool_v4_onepiece.step / .stl`

## 主要尺寸

- 上部主体外径：{PARAMS["upper_body_diameter_mm"]:.1f} mm
- 下部圆柱外径：{PARAMS["lower_body_diameter_mm"]:.1f} mm
- 接触垫底面外径：{PARAMS["pad_bottom_diameter_mm"]:.1f} mm
- 接触垫高度：{PARAMS["pad_height_mm"]:.1f} mm
- 接触面距法兰面轴向距离：{verification["derived"]["contact_face_from_flange_face_mm"]:.1f} mm

## 说明

- `onepiece` 适合快速看整体比例或直接打一体件
- 分件版 `body + pad` 更适合后面改单独接触垫
- 机器人侧接口仍沿用前一版的 `50-4-M6`
"""
    (VERSION_DIR / "README.md").write_text(readme, encoding="utf-8")

    print(VERSION_DIR / "ur5e_flat_pad_tool_v4_onepiece.step")
    print(VERSION_DIR / "verification.json")


if __name__ == "__main__":
    main()
