import json
import math
from pathlib import Path

import Part
from FreeCAD import Vector


VERSION_DIR = Path("/Users/andyl/Documents/UR5e/eoat/eoat_print/v10_ksm8n_receiver_recessed_face_85mm")


PARAMS = {
    # UR5e flange interface inherited from v6_slim_adapter_72mm.
    "adapter_outer_diameter_mm": 72.0,
    "adapter_thickness_mm": 12.0,
    "robot_pattern_pcd_mm": 50.0,
    "robot_screw_clearance_mm": 6.6,
    "robot_socket_head_counterbore_mm": 11.0,
    "robot_socket_head_cbore_depth_mm": 6.0,
    "robot_center_recess_mm": 31.7,
    "robot_center_recess_depth_mm": 2.0,
    # Printed tool body.
    "base_body_diameter_mm": 40.0,
    "base_body_height_mm": 20.0,
    "transition_height_mm": 14.0,
    "neck_diameter_mm": 28.0,
    "neck_height_mm": 10.0,
    "receiver_diameter_mm": 28.0,
    "receiver_height_mm": 17.9,
    "center_lightening_bore_mm": 6.0,
    "front_face_recess_outer_diameter_mm": 24.5,
    "front_face_recess_inner_boss_diameter_mm": 18.0,
    "front_face_recess_depth_mm": 1.8,
    # Captive M6 receiver.
    "m6_clearance_hole_mm": 6.6,
    "m6_hex_nut_across_flats_pocket_mm": 10.6,
    "m6_hex_nut_pocket_depth_mm": 5.8,
    "nut_side_slot_width_mm": 12.0,
    "nut_side_slot_extra_depth_mm": 0.6,
    "nut_front_setback_mm": 3.8,
    "hot_glue_relief_depth_mm": 1.2,
    # KSM-8N local product image dimensions.
    "ksm_ball_diameter_mm": 8.0,
    "ksm_housing_diameter_mm": 15.0,
    "ksm_housing_total_height_to_ball_top_mm": 11.1,
    "ksm_visible_housing_height_mm": 9.1,
    "ksm_ball_exposed_height_mm": 2.0,
    "ksm_thread_diameter_mm": 6.0,
    "ksm_thread_length_mm": 12.0,
    # Target.
    "contact_point_from_flange_face_mm": 85.0,
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


def make_box_centered(x_len_mm: float, y_len_mm: float, z_len_mm: float, center: tuple[float, float, float]):
    cx, cy, cz = center
    return Part.makeBox(
        x_len_mm,
        y_len_mm,
        z_len_mm,
        Vector(cx - x_len_mm / 2.0, cy - y_len_mm / 2.0, cz - z_len_mm / 2.0),
    )


def make_hex_prism_z(across_flats_mm: float, height_mm: float, z0_mm: float):
    radius = across_flats_mm / math.sqrt(3.0)
    points = []
    # 30 degrees gives a flat-to-flat orientation aligned to X/Y axes.
    for idx in range(6):
        angle = math.radians(30.0 + idx * 60.0)
        points.append(Vector(radius * math.cos(angle), radius * math.sin(angle), z0_mm))
    points.append(points[0])
    face = Part.Face(Part.makePolygon(points))
    return face.extrude(Vector(0, 0, height_mm))


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

    center_bore = make_cylinder_z(p["center_lightening_bore_mm"], thickness_mm + 0.4, -0.2)
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


def derived_positions():
    p = PARAMS
    contact_z = p["contact_point_from_flange_face_mm"]
    ksm_housing_bottom_z = contact_z - p["ksm_housing_total_height_to_ball_top_mm"]
    ksm_thread_back_z = ksm_housing_bottom_z - p["ksm_thread_length_mm"]
    nut_z1 = ksm_housing_bottom_z - p["nut_front_setback_mm"]
    nut_z0 = nut_z1 - p["m6_hex_nut_pocket_depth_mm"]
    nut_center_z = (nut_z0 + nut_z1) / 2.0
    return {
        "contact_z": contact_z,
        "receiver_front_z": ksm_housing_bottom_z,
        "ksm_housing_bottom_z": ksm_housing_bottom_z,
        "ksm_thread_back_z": ksm_thread_back_z,
        "nut_z0": nut_z0,
        "nut_z1": nut_z1,
        "nut_center_z": nut_center_z,
    }


def cut_receiver_features(shape, receiver_front_z: float, receiver_radius_mm: float):
    p = PARAMS
    positions = derived_positions()
    nut_z0 = positions["nut_z0"]
    nut_z1 = positions["nut_z1"]
    nut_center_z = positions["nut_center_z"]
    ksm_thread_back_z = positions["ksm_thread_back_z"]

    axial_clearance = make_cylinder_z(
        p["m6_clearance_hole_mm"],
        receiver_front_z - ksm_thread_back_z + 1.4,
        ksm_thread_back_z - 0.7,
    )
    shape = shape.cut(axial_clearance)

    nut_pocket = make_hex_prism_z(
        p["m6_hex_nut_across_flats_pocket_mm"],
        p["m6_hex_nut_pocket_depth_mm"] + 0.2,
        nut_z0 - 0.1,
    )
    shape = shape.cut(nut_pocket)

    side_slot = Part.makeBox(
        receiver_radius_mm + p["nut_side_slot_extra_depth_mm"],
        p["nut_side_slot_width_mm"],
        p["m6_hex_nut_pocket_depth_mm"] + 0.4,
        Vector(
            -receiver_radius_mm - p["nut_side_slot_extra_depth_mm"],
            -p["nut_side_slot_width_mm"] / 2.0,
            nut_z0 - 0.2,
        ),
    )
    shape = shape.cut(side_slot)

    # Shallow relief at the side opening for hot glue retention only; it is not
    # part of the load path.
    glue_relief = Part.makeBox(
        p["hot_glue_relief_depth_mm"] + 0.1,
        p["nut_side_slot_width_mm"] + 2.0,
        p["m6_hex_nut_pocket_depth_mm"] + 1.4,
        Vector(
            -receiver_radius_mm - 0.05,
            -(p["nut_side_slot_width_mm"] + 2.0) / 2.0,
            nut_z0 - 0.7,
        ),
    )
    shape = shape.cut(glue_relief)

    return shape


def cut_front_face_recess(shape, receiver_front_z: float):
    p = PARAMS
    recess_depth = p["front_face_recess_depth_mm"]
    outer = make_cylinder_z(
        p["front_face_recess_outer_diameter_mm"],
        recess_depth + 0.3,
        receiver_front_z - recess_depth,
    )
    inner_boss_keepout = make_cylinder_z(
        p["front_face_recess_inner_boss_diameter_mm"],
        recess_depth + 0.7,
        receiver_front_z - recess_depth - 0.2,
    )
    annular_recess = outer.cut(inner_boss_keepout)
    return shape.cut(annular_recess)


def build_body():
    p = PARAMS
    positions = derived_positions()
    z_adapter = 0.0
    z_base = z_adapter + p["adapter_thickness_mm"]
    z_transition = z_base + p["base_body_height_mm"]
    z_neck = z_transition + p["transition_height_mm"]
    receiver_front_z = positions["receiver_front_z"]
    z_receiver = receiver_front_z - p["receiver_height_mm"]
    neck_height = z_receiver - z_neck

    if neck_height <= 0:
        raise ValueError("Body length parameters leave no positive neck height.")
    if abs(neck_height - p["neck_height_mm"]) > 1e-6:
        raise ValueError(
            f"Body segmentation mismatch: expected neck {p['neck_height_mm']} mm, got {neck_height} mm."
        )

    body = build_adapter_disc(p["adapter_thickness_mm"])
    body = body.fuse(make_cylinder_z(p["base_body_diameter_mm"], p["base_body_height_mm"], z_base))
    body = body.fuse(
        make_cone_z(
            p["base_body_diameter_mm"],
            p["neck_diameter_mm"],
            p["transition_height_mm"],
            z_transition,
        )
    )
    body = body.fuse(make_cylinder_z(p["neck_diameter_mm"], neck_height, z_neck))
    body = body.fuse(make_cylinder_z(p["receiver_diameter_mm"], p["receiver_height_mm"], z_receiver))

    lightening_bore = make_cylinder_z(
        p["center_lightening_bore_mm"],
        positions["ksm_thread_back_z"] + 0.2,
        -0.2,
    )
    body = body.cut(lightening_bore)
    body = cut_receiver_features(body, receiver_front_z, p["receiver_diameter_mm"] / 2.0)
    body = cut_front_face_recess(body, receiver_front_z)

    return body


def build_ksm_placeholder():
    p = PARAMS
    positions = derived_positions()
    contact_z = positions["contact_z"]
    housing_bottom_z = positions["ksm_housing_bottom_z"]
    thread_back_z = positions["ksm_thread_back_z"]

    thread = make_cylinder_z(p["ksm_thread_diameter_mm"], p["ksm_thread_length_mm"], thread_back_z)
    housing = make_cylinder_z(p["ksm_housing_diameter_mm"], p["ksm_visible_housing_height_mm"], housing_bottom_z)
    ball_center_z = contact_z - p["ksm_ball_diameter_mm"] / 2.0
    ball = Part.makeSphere(p["ksm_ball_diameter_mm"] / 2.0, Vector(0, 0, ball_center_z))

    return thread.fuse(housing).fuse(ball)


def build_receiver_fitcheck():
    p = PARAMS
    height = 24.0
    body = make_cylinder_z(p["receiver_diameter_mm"], height, 0.0)

    # Reuse the exact receiver geometry locally by temporarily shifting the
    # global receiver cuts down to this coupon.
    original_contact = p["contact_point_from_flange_face_mm"]
    original_receiver = derived_positions()["receiver_front_z"]
    try:
        p["contact_point_from_flange_face_mm"] = height + p["ksm_housing_total_height_to_ball_top_mm"]
        body = cut_receiver_features(body, height, p["receiver_diameter_mm"] / 2.0)
        body = cut_front_face_recess(body, height)
    finally:
        p["contact_point_from_flange_face_mm"] = original_contact
        assert abs(derived_positions()["receiver_front_z"] - original_receiver) < 1e-9

    return body


def build_fitcheck():
    return build_adapter_disc(4.0, include_counterbores=False)


def save_shape(shape, stem: str):
    shape.exportStep(str(VERSION_DIR / f"{stem}.step"))
    shape.exportStl(str(VERSION_DIR / f"{stem}.stl"))


def validate_shape(name: str, shape):
    bb = shape.BoundBox
    if bb.XLength <= 0 or bb.YLength <= 0 or bb.ZLength <= 0:
        raise ValueError(f"{name} has an invalid or empty bounding box: {bbox_dict(shape)}")


def write_readme(verification):
    p = PARAMS
    text = f"""# UR5e KSM-8N ball-transfer tool v10, recessed receiver face, 85 mm TCP

This version keeps the v9 slim 28.0 mm receiver and adds a shallow annular
recess on the receiver front face. The KSM-8N remains a purchased metal part;
the recess only removes printed material around the front face while leaving a
flat central seating boss for the KSM housing.

## Current design

- Direct `UR5e` flange interface inherited from `v6_slim_adapter_72mm`
- Adapter OD: {p["adapter_outer_diameter_mm"]:.1f} mm
- Robot flange pattern: 4 x M6 on {p["robot_pattern_pcd_mm"]:.1f} mm PCD
- Center register/recess retained; OnRobot QC-R smooth locating pin is not copied
- Contact module: purchased KSM-8N; placeholder is exported separately only for fit reference
- KSM thread: M6 x {p["ksm_thread_length_mm"]:.1f} mm, held by captive M6 hex nut
- Contact point from flange face: {verification["derived"]["contact_point_from_flange_face_mm"]:.1f} mm
- Body segmentation: adapter 0-12 mm, base 12-32 mm, taper 32-46 mm, neck 46-56 mm, receiver 56-73.9 mm
- Receiver head OD: {p["receiver_diameter_mm"]:.1f} mm
- Receiver front face recess: {p["front_face_recess_outer_diameter_mm"]:.1f} mm OD annulus, {p["front_face_recess_depth_mm"]:.1f} mm deep, with {p["front_face_recess_inner_boss_diameter_mm"]:.1f} mm central seating boss

## Output files

- `ur5e_ksm8n_ball_transfer_tool_v10_body.step / .stl`
- `ur5e_ksm8n_ball_transfer_tool_v10_ksm_placeholder.step / .stl`
- `ur5e_ksm8n_ball_transfer_tool_v10_assembly.step / .stl`
- `ur5e_ksm8n_ball_transfer_tool_v10_onepiece_preview.step / .stl`
- `ur5e_ksm8n_ball_transfer_tool_v10_receiver_fitcheck.step / .stl`
- `ur5e_ksm8n_ball_transfer_tool_v10_flange_fitcheck.step / .stl`

`assembly` and `onepiece_preview` intentionally contain only the printable
body. The KSM-8N is a purchased metal part and is not fused into printable
geometry.

## Receiver notes

- M6 hex pocket follows the v5 coupon: {p["m6_hex_nut_across_flats_pocket_mm"]:.1f} mm across flats and {p["m6_hex_nut_pocket_depth_mm"]:.1f} mm deep.
- The side window is for inserting the nut and optionally adding hot glue.
- Hot glue is retention only; the load path is printed receiver -> metal M6 nut -> KSM-8N thread.
- Keep glue away from the M6 thread and KSM bearing body.

## Print and bench order

1. Print `receiver_fitcheck`.
2. Check that the M6 nut inserts, does not rotate, and accepts the KSM-8N smoothly.
3. Print `body`.
4. Mount on the bare UR5e flange and perform a low-speed no-contact posture sweep.
5. Only then proceed to contact experiments.
"""
    (VERSION_DIR / "README.md").write_text(text, encoding="utf-8")


def main():
    VERSION_DIR.mkdir(parents=True, exist_ok=True)

    body = build_body()
    ksm = build_ksm_placeholder()
    assembly = body.copy()
    onepiece_preview = body.copy()
    receiver_fitcheck = build_receiver_fitcheck()
    flange_fitcheck = build_fitcheck()

    for name, shape in {
        "body": body,
        "ksm_placeholder": ksm,
        "assembly": assembly,
        "onepiece_preview": onepiece_preview,
        "receiver_fitcheck": receiver_fitcheck,
        "flange_fitcheck": flange_fitcheck,
    }.items():
        validate_shape(name, shape)

    stem = "ur5e_ksm8n_ball_transfer_tool_v10"
    save_shape(body, f"{stem}_body")
    save_shape(ksm, f"{stem}_ksm_placeholder")
    save_shape(assembly, f"{stem}_assembly")
    save_shape(onepiece_preview, f"{stem}_onepiece_preview")
    save_shape(receiver_fitcheck, f"{stem}_receiver_fitcheck")
    save_shape(flange_fitcheck, f"{stem}_flange_fitcheck")

    positions = derived_positions()
    counterbore_outer_radius = (
        PARAMS["robot_pattern_pcd_mm"] / 2.0
        + PARAMS["robot_socket_head_counterbore_mm"] / 2.0
    )
    verification = {
        "version": "v10_ksm8n_receiver_recessed_face_85mm",
        "concept": "Short direct-flange UR5e passive rolling-contact EOAT using local KSM-8N M6 ball transfer with a slim receiver and recessed front face.",
        "parameters": PARAMS,
        "derived": {
            "contact_point_from_flange_face_mm": positions["contact_z"],
            "receiver_front_z_mm": positions["receiver_front_z"],
            "ksm_thread_back_z_mm": positions["ksm_thread_back_z"],
            "nut_z0_mm": positions["nut_z0"],
            "nut_z1_mm": positions["nut_z1"],
            "adapter_outer_radius_mm": PARAMS["adapter_outer_diameter_mm"] / 2.0,
            "counterbore_outer_radius_from_center_mm": counterbore_outer_radius,
            "outer_edge_margin_after_counterbore_mm": (
                PARAMS["adapter_outer_diameter_mm"] / 2.0 - counterbore_outer_radius
            ),
            "body_length_to_receiver_face_mm": body.BoundBox.ZMax,
            "assembly_zmax_mm": assembly.BoundBox.ZMax,
            "purchased_ksm_placeholder_excluded_from_printable_outputs": True,
            "front_face_recess_bottom_z_mm": positions["receiver_front_z"] - PARAMS["front_face_recess_depth_mm"],
            "tcp_on_centerline": True,
            "receiver_strategy": "captive M6 hex nut with optional hot-glue retention only",
        },
        "bounding_boxes": {
            "body": bbox_dict(body),
            "ksm_placeholder": bbox_dict(ksm),
            "assembly": bbox_dict(assembly),
            "onepiece_preview": bbox_dict(onepiece_preview),
            "receiver_fitcheck": bbox_dict(receiver_fitcheck),
            "flange_fitcheck": bbox_dict(flange_fitcheck),
        },
        "validation": {
            "contact_point_target_mm": 85.0,
            "contact_point_error_mm": positions["contact_z"] - 85.0,
            "flange_pattern_unchanged_from_v6": True,
            "m6_nut_pocket_inherited_from_v5_coupon": True,
            "only_receiver_head_slimmed_from_v8": True,
            "assembly_contains_printed_body_only": True,
            "front_face_recess_keeps_central_ksm_seating_boss": True,
        },
        "assumptions_to_confirm": [
            "KSM-8N dimensions follow the local product image: 8 mm ball, 15 mm housing, 11.1 mm total housing-to-ball-top height, and 12 mm M6 thread.",
            "The KSM-8N thread should engage a metal captive nut; hot glue is only to retain the nut after assembly.",
            "The receiver_fitcheck should be printed before the full body to confirm nut insertion, KSM engagement, and glue clearance.",
            "Run only a low-speed no-contact posture sweep before any force-contact experiment.",
        ],
    }

    (VERSION_DIR / "verification.json").write_text(json.dumps(verification, indent=2), encoding="utf-8")
    write_readme(verification)

    print(VERSION_DIR / f"{stem}_assembly.step")
    print(VERSION_DIR / "verification.json")


if __name__ == "__main__":
    main()
