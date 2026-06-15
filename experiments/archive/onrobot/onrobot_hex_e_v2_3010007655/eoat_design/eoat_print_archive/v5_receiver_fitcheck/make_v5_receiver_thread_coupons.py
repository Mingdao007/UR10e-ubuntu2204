#!/usr/bin/env python3
"""Generate UR5e EOAT v5 receiver thread fit-check coupons.

The coupons are bench-only parts. They do not mount to the UR5e.
They compare two M6 thread strategies for KSM / ball-plunger cartridges:

1. Captive M6 hex nut pocket.
2. Plastic self-threading pilot holes.
"""

from __future__ import annotations

import json
from pathlib import Path

from build123d import (
    Align,
    Box,
    BuildPart,
    BuildSketch,
    Cylinder,
    Locations,
    Mode,
    Plane,
    RegularPolygon,
    Rotation,
    export_step,
    export_stl,
    extrude,
)


OUT = Path(__file__).resolve().parent

PARAMS = {
    "coupon_length_x_mm": 42.0,
    "coupon_width_y_mm": 34.0,
    "coupon_height_z_mm": 20.0,
    "m6_clearance_hole_mm": 6.6,
    "m6_hex_nut_across_flats_pocket_mm": 10.6,
    "m6_hex_nut_pocket_depth_mm": 5.8,
    "self_thread_pilot_holes_mm": [5.0, 5.2, 5.4],
    "self_thread_hole_spacing_y_mm": 10.0,
}


def hex_vertex_radius_from_across_flats(across_flats: float) -> float:
    """Regular hexagon vertex radius for a target across-flats dimension."""
    return across_flats / (3.0**0.5)


def build_captive_nut_coupon():
    p = PARAMS
    length = p["coupon_length_x_mm"]
    width = p["coupon_width_y_mm"]
    height = p["coupon_height_z_mm"]
    nut_depth = p["m6_hex_nut_pocket_depth_mm"]
    nut_r = hex_vertex_radius_from_across_flats(
        p["m6_hex_nut_across_flats_pocket_mm"]
    )

    with BuildPart() as part:
        Box(
            length,
            width,
            height,
            align=(Align.CENTER, Align.CENTER, Align.CENTER),
        )

        # M6 screw/accessory enters along X and engages the captive metal nut.
        with Locations((0, 0, 0)):
            Cylinder(
                p["m6_clearance_hole_mm"] / 2.0,
                length + 1.0,
                rotation=Rotation(0, 90, 0),
                mode=Mode.SUBTRACT,
            )

        # Back/side-loaded M6 nut socket. The hex flats prevent nut rotation.
        with BuildSketch(Plane.YZ.offset(-length / 2.0)):
            RegularPolygon(nut_r, 6, major_radius=True, rotation=30)
        extrude(amount=nut_depth + 0.2, mode=Mode.SUBTRACT)

    return part.part


def build_self_thread_coupon():
    p = PARAMS
    length = p["coupon_length_x_mm"]
    width = p["coupon_width_y_mm"]
    height = p["coupon_height_z_mm"]
    holes = p["self_thread_pilot_holes_mm"]
    spacing = p["self_thread_hole_spacing_y_mm"]
    y0 = -spacing

    with BuildPart() as part:
        Box(
            length,
            width,
            height,
            align=(Align.CENTER, Align.CENTER, Align.CENTER),
        )

        for i, dia in enumerate(holes):
            y = y0 + i * spacing
            with Locations((0, y, 0)):
                Cylinder(
                    dia / 2.0,
                    length + 1.0,
                    rotation=Rotation(0, 90, 0),
                    mode=Mode.SUBTRACT,
                )

    return part.part


def export_shape(shape, stem: str):
    export_step(shape, OUT / f"{stem}.step")
    export_stl(shape, OUT / f"{stem}.stl", tolerance=0.005, angular_tolerance=0.1)


def main():
    captive = build_captive_nut_coupon()
    self_thread = build_self_thread_coupon()

    export_shape(captive, "v5_receiver_fitcheck_captive_m6_nut")
    export_shape(self_thread, "v5_receiver_fitcheck_self_thread_m6_pilots")

    verification = {
        "version": "v5_receiver_fitcheck",
        "purpose": "Bench-only receiver/thread strategy coupons, not robot-mounted parts.",
        "parameters": PARAMS,
        "parts": {
            "v5_receiver_fitcheck_captive_m6_nut": {
                "use": "Insert an M6 hex nut from the back/side pocket; screw KSM or M6 ball plunger through the front clearance hole into the metal nut.",
                "nut_pocket": "M6 hex pocket, 10.6 mm across flats, 5.8 mm deep.",
                "expected_feedback": [
                    "Does the M6 nut press in by hand?",
                    "Does the nut rotate when tightening?",
                    "Does the KSM / ball plunger thread engage smoothly?",
                ],
            },
            "v5_receiver_fitcheck_self_thread_m6_pilots": {
                "use": "Try the KSM or M6 ball plunger in the three pilot holes. Mark which hole feels best.",
                "pilot_holes_mm": PARAMS["self_thread_pilot_holes_mm"],
                "hole_order_when_viewed_from_front": "left to right by Y position: 5.0, 5.2, 5.4 mm",
                "expected_feedback": [
                    "Which pilot diameter starts without cracking?",
                    "Which pilot diameter holds without wobble?",
                    "How much torque/force does insertion require?",
                ],
            },
        },
        "notes": [
            "The self-threading coupon is for quick comparison only; do not assume it is durable enough for the final contact head.",
            "The captive-nut coupon is the preferred functional direction if repeated tool changes are expected.",
            "Glue is optional only to stop an unloaded nut from falling out; the load path should be the printed geometry plus metal nut.",
        ],
    }
    (OUT / "verification.json").write_text(
        json.dumps(verification, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
