from math import cos, radians, sin
from pathlib import Path
from typing import Optional

from build123d import (
    Align,
    Box,
    BuildPart,
    Compound,
    Cylinder,
    Location,
    Locations,
    Mode,
    Sphere,
    export_step,
    export_stl,
)


VERSION_DIR = Path("/Users/andyl/Documents/UR5e/eoat_print/v2")
PARTS_DIR = VERSION_DIR / "parts"
ASSEMBLY_DIR = VERSION_DIR / "assembly"
PREVIEW_DIR = VERSION_DIR / "preview"
NOTES_DIR = VERSION_DIR / "notes"


PARAMS = {
    # UR5e flange adapter
    "adapter_outer_diameter_mm": 80.0,
    "adapter_thickness_mm": 12.0,
    "robot_pattern_pcd_mm": 50.0,
    "robot_screw_clearance_mm": 6.6,
    "robot_socket_head_counterbore_mm": 11.0,
    "robot_socket_head_cbore_depth_mm": 6.0,
    "robot_center_recess_mm": 31.7,
    "robot_center_recess_depth_mm": 2.0,
    # Concept EOAT body
    "body_outer_diameter_mm": 50.0,
    "body_height_mm": 34.0,
    "nose_outer_diameter_mm": 38.0,
    "nose_height_mm": 16.0,
    "side_flat_width_mm": 14.0,
    # Internal guidance / compliance zone
    "guide_bore_mm": 8.4,
    "plunger_bore_mm": 16.0,
    "plunger_bore_depth_mm": 28.0,
    # Probe
    "probe_shaft_diameter_mm": 8.0,
    "probe_exposed_length_mm": 62.0,
    "probe_tip_diameter_mm": 10.0,
    "probe_plunger_diameter_mm": 14.0,
    "probe_plunger_length_mm": 14.0,
}


def polar_points(radius_mm: float, count: int, offset_deg: float = 0.0):
    points = []
    for idx in range(count):
        angle_deg = offset_deg + idx * 360.0 / count
        angle = radians(angle_deg)
        points.append((radius_mm * cos(angle), radius_mm * sin(angle)))
    return points


def build_adapter_disc(
    thickness_override_mm: Optional[float] = None,
    outer_diameter_override_mm: Optional[float] = None,
):
    p = PARAMS
    thickness = thickness_override_mm or p["adapter_thickness_mm"]
    outer_diameter = outer_diameter_override_mm or p["adapter_outer_diameter_mm"]
    outer_radius = outer_diameter / 2.0

    with BuildPart() as adapter:
        Cylinder(
            outer_radius,
            thickness,
            align=(Align.CENTER, Align.CENTER, Align.MIN),
        )

        # Robot-side centering recess.
        Cylinder(
            p["robot_center_recess_mm"] / 2.0,
            p["robot_center_recess_depth_mm"] + 0.2,
            align=(Align.CENTER, Align.CENTER, Align.MIN),
            mode=Mode.SUBTRACT,
        )

        # Main center pass-through.
        Cylinder(
            p["guide_bore_mm"] / 2.0,
            thickness + 0.4,
            align=(Align.CENTER, Align.CENTER, Align.MIN),
            mode=Mode.SUBTRACT,
        )

        robot_radius = p["robot_pattern_pcd_mm"] / 2.0
        for x, y in polar_points(robot_radius, 4, offset_deg=45.0):
            with Locations((x, y, -0.2)):
                Cylinder(
                    p["robot_screw_clearance_mm"] / 2.0,
                    thickness + 0.4,
                    align=(Align.CENTER, Align.CENTER, Align.MIN),
                    mode=Mode.SUBTRACT,
                )
            with Locations((x, y, thickness - p["robot_socket_head_cbore_depth_mm"])):
                Cylinder(
                    p["robot_socket_head_counterbore_mm"] / 2.0,
                    p["robot_socket_head_cbore_depth_mm"] + 0.2,
                    align=(Align.CENTER, Align.CENTER, Align.MIN),
                    mode=Mode.SUBTRACT,
                )

    return adapter.part


def build_fit_check_plate():
    """Thin validation plate for UR5e flange fit confirmation before full prints."""
    return build_adapter_disc(thickness_override_mm=4.0, outer_diameter_override_mm=80.0)


def build_base_body():
    p = PARAMS
    adapter_thickness = p["adapter_thickness_mm"]
    body_z = adapter_thickness
    nose_z = adapter_thickness + p["body_height_mm"]
    total_height = adapter_thickness + p["body_height_mm"] + p["nose_height_mm"]

    with BuildPart() as body:
        # Adapter plate
        Cylinder(
            p["adapter_outer_diameter_mm"] / 2.0,
            adapter_thickness,
            align=(Align.CENTER, Align.CENTER, Align.MIN),
        )

        Cylinder(
            p["robot_center_recess_mm"] / 2.0,
            p["robot_center_recess_depth_mm"] + 0.2,
            align=(Align.CENTER, Align.CENTER, Align.MIN),
            mode=Mode.SUBTRACT,
        )

        robot_radius = p["robot_pattern_pcd_mm"] / 2.0
        for x, y in polar_points(robot_radius, 4, offset_deg=45.0):
            with Locations((x, y, -0.2)):
                Cylinder(
                    p["robot_screw_clearance_mm"] / 2.0,
                    adapter_thickness + 0.4,
                    align=(Align.CENTER, Align.CENTER, Align.MIN),
                    mode=Mode.SUBTRACT,
                )
            with Locations((x, y, adapter_thickness - p["robot_socket_head_cbore_depth_mm"])):
                Cylinder(
                    p["robot_socket_head_counterbore_mm"] / 2.0,
                    p["robot_socket_head_cbore_depth_mm"] + 0.2,
                    align=(Align.CENTER, Align.CENTER, Align.MIN),
                    mode=Mode.SUBTRACT,
                )

        # Main cylindrical housing
        with Locations((0, 0, body_z)):
            Cylinder(
                p["body_outer_diameter_mm"] / 2.0,
                p["body_height_mm"],
                align=(Align.CENTER, Align.CENTER, Align.MIN),
            )

        # Flat side tabs to make it look less like a raw can
        side_offset = p["body_outer_diameter_mm"] / 2.0 - p["side_flat_width_mm"] / 2.0
        for sign in (-1, 1):
            with Locations((sign * side_offset, 0, body_z + p["body_height_mm"] / 2.0)):
                Box(
                    p["side_flat_width_mm"],
                    12.0,
                    18.0,
                    align=(Align.CENTER, Align.CENTER, Align.CENTER),
                )

        # Front nose / probe holder
        with Locations((0, 0, nose_z)):
            Cylinder(
                p["nose_outer_diameter_mm"] / 2.0,
                p["nose_height_mm"],
                align=(Align.CENTER, Align.CENTER, Align.MIN),
            )

        # Through guide for the probe shaft
        Cylinder(
            p["guide_bore_mm"] / 2.0,
            total_height + 0.4,
            align=(Align.CENTER, Align.CENTER, Align.MIN),
            mode=Mode.SUBTRACT,
        )

        # Larger plunger cavity from the tool side
        with Locations((0, 0, total_height - p["plunger_bore_depth_mm"])):
            Cylinder(
                p["plunger_bore_mm"] / 2.0,
                p["plunger_bore_depth_mm"] + 0.4,
                align=(Align.CENTER, Align.CENTER, Align.MIN),
                mode=Mode.SUBTRACT,
            )

    return body.part


def build_probe():
    p = PARAMS
    with BuildPart() as probe:
        # Plunger section inside the compliance cavity
        with Locations((0, 0, 0)):
            Cylinder(
                p["probe_plunger_diameter_mm"] / 2.0,
                p["probe_plunger_length_mm"],
                align=(Align.CENTER, Align.CENTER, Align.MIN),
            )

        # Main exposed shaft
        with Locations((0, 0, p["probe_plunger_length_mm"])):
            Cylinder(
                p["probe_shaft_diameter_mm"] / 2.0,
                p["probe_exposed_length_mm"],
                align=(Align.CENTER, Align.CENTER, Align.MIN),
            )

        # Rounded contact tip
        with Locations((0, 0, p["probe_plunger_length_mm"] + p["probe_exposed_length_mm"])):
            Sphere(p["probe_tip_diameter_mm"] / 2.0)

    return probe.part


def build_concept_assembly():
    p = PARAMS
    base = build_base_body()
    probe = build_probe()
    total_height = p["adapter_thickness_mm"] + p["body_height_mm"] + p["nose_height_mm"]

    # Place the plunger partially inside the cavity and expose the shaft below.
    probe_origin_z = total_height - p["plunger_bore_depth_mm"] + 6.0
    probe = probe.moved(Location((0, 0, probe_origin_z)))

    assembly = Compound(children=[base, probe])
    onepiece = base.fuse(probe)
    return base, probe, assembly, onepiece


def main():
    PARTS_DIR.mkdir(parents=True, exist_ok=True)
    ASSEMBLY_DIR.mkdir(parents=True, exist_ok=True)
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    NOTES_DIR.mkdir(parents=True, exist_ok=True)

    base, probe, assembly, onepiece = build_concept_assembly()
    fit_check = build_fit_check_plate()

    export_stl(base, str(PARTS_DIR / "ur5e_probe_concept_v2_base.stl"))
    export_step(base, str(PARTS_DIR / "ur5e_probe_concept_v2_base.step"))
    export_stl(probe, str(PARTS_DIR / "ur5e_probe_concept_v2_probe.stl"))
    export_step(probe, str(PARTS_DIR / "ur5e_probe_concept_v2_probe.step"))
    export_stl(fit_check, str(PARTS_DIR / "ur5e_probe_concept_v2_fitcheck.stl"))
    export_step(fit_check, str(PARTS_DIR / "ur5e_probe_concept_v2_fitcheck.step"))
    export_stl(assembly, str(ASSEMBLY_DIR / "ur5e_probe_concept_v2_assembly.stl"))
    export_step(assembly, str(ASSEMBLY_DIR / "ur5e_probe_concept_v2_assembly.step"))
    export_stl(onepiece, str(ASSEMBLY_DIR / "ur5e_probe_concept_v2_onepiece.stl"))
    export_step(onepiece, str(ASSEMBLY_DIR / "ur5e_probe_concept_v2_onepiece.step"))

    print(PARTS_DIR / "ur5e_probe_concept_v2_base.stl")
    print(PARTS_DIR / "ur5e_probe_concept_v2_probe.stl")
    print(PARTS_DIR / "ur5e_probe_concept_v2_fitcheck.stl")
    print(ASSEMBLY_DIR / "ur5e_probe_concept_v2_assembly.step")
    print(ASSEMBLY_DIR / "ur5e_probe_concept_v2_onepiece.stl")


if __name__ == "__main__":
    main()
