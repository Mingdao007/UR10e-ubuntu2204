"""Load and assess the offline TacDiffusion surface-input manifest.

The manifest is deliberately separate from ``SurfaceCalibration``.  The
current four-corner evidence is a curved/non-planar surface, so it must be
assessed before a planar trajectory backend is allowed to consume it.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any, Sequence

from .surface import SurfaceCalibration, SurfacePose, calibrate_surface


EXPECTED_SCHEMA = "ur10e_tacdiffusion_surface_input/v1"
EXPECTED_CORNER_ORDER = ("right_upper", "left_upper", "left_lower", "right_lower")


def _finite_vector(values: Sequence[Any], size: int, name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) != size or not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} must contain {size} finite values")
    return result


def rotvec_to_quaternion_xyzw(rotvec_rad: Sequence[float]) -> tuple[float, float, float, float]:
    """Convert a UR pose rotation vector in radians to a unit xyzw quaternion."""

    rx, ry, rz = _finite_vector(rotvec_rad, 3, "rotvec_rad")
    angle = math.sqrt(rx * rx + ry * ry + rz * rz)
    if angle <= 1e-12:
        return (0.0, 0.0, 0.0, 1.0)
    half_angle = 0.5 * angle
    scale = math.sin(half_angle) / angle
    return (rx * scale, ry * scale, rz * scale, math.cos(half_angle))


@dataclass(frozen=True)
class SurfaceInputSet:
    schema: str
    frame_id: str
    corner_order: tuple[str, ...]
    corners: tuple[SurfacePose, ...]
    manifest: dict[str, Any]


@dataclass(frozen=True)
class SurfaceInputAssessment:
    width_m: float
    height_m: float
    orthogonality_cosine: float
    planarity_residual_m: float
    rectangle_closure_error_m: float
    z_range_m: float
    orientation_spread_rad: float
    planar_contract_eligible: bool


def load_surface_input_manifest(path: str | Path) -> SurfaceInputSet:
    """Load a self-contained corner manifest without touching robot I/O."""

    manifest_path = Path(path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != EXPECTED_SCHEMA:
        raise ValueError(f"unsupported surface input schema: {manifest.get('schema')!r}")
    convention = manifest.get("coordinate_convention")
    if not isinstance(convention, dict):
        raise ValueError("coordinate_convention is required")
    if convention.get("frame_id") != "base":
        raise ValueError("TacDiffusion surface input must use the UR base frame")
    if convention.get("position_units") != "m" or convention.get("orientation_units") != "rad":
        raise ValueError("surface input units must be metres and radians")
    if convention.get("orientation_parameterization") != "ur_rotvec":
        raise ValueError("surface orientation must use UR rotation vectors")
    order = tuple(convention.get("corner_order", ()))
    if order != EXPECTED_CORNER_ORDER:
        raise ValueError(f"corner order must be {EXPECTED_CORNER_ORDER}")

    raw_corners = manifest.get("corners")
    if not isinstance(raw_corners, list) or len(raw_corners) != 4:
        raise ValueError("surface input requires exactly four corners")
    corners: list[SurfacePose] = []
    labels: list[str] = []
    for expected_label, raw in zip(EXPECTED_CORNER_ORDER, raw_corners):
        if not isinstance(raw, dict) or raw.get("user_label") != expected_label:
            raise ValueError("corner labels do not match the canonical order")
        pose = _finite_vector(raw.get("tcp_pose", ()), 6, f"{expected_label}.tcp_pose")
        labels.append(expected_label)
        corners.append(
            SurfacePose(
                position_base_m=pose[:3],
                orientation_xyzw=rotvec_to_quaternion_xyzw(pose[3:]),
                frame_id="base",
            )
        )
    return SurfaceInputSet(EXPECTED_SCHEMA, "base", tuple(labels), tuple(corners), manifest)


def assess_surface_input(surface_input: SurfaceInputSet) -> SurfaceInputAssessment:
    """Compute geometry diagnostics used by the planar fail-closed gate."""

    positions = [corner.position_base_m for corner in surface_input.corners]
    p0, p1, p2, p3 = positions
    edge_u = tuple(p1[index] - p0[index] for index in range(3))
    edge_v = tuple(p3[index] - p0[index] for index in range(3))
    width = math.sqrt(sum(value * value for value in edge_u))
    height = math.sqrt(sum(value * value for value in edge_v))
    dot = sum(edge_u[index] * edge_v[index] for index in range(3))
    cosine = dot / (width * height)
    cross = (
        edge_u[1] * edge_v[2] - edge_u[2] * edge_v[1],
        edge_u[2] * edge_v[0] - edge_u[0] * edge_v[2],
        edge_u[0] * edge_v[1] - edge_u[1] * edge_v[0],
    )
    cross_norm = math.sqrt(sum(value * value for value in cross))
    normal = tuple(value / cross_norm for value in cross)
    p2_delta = tuple(p2[index] - p0[index] for index in range(3))
    expected_p2 = tuple(p1[index] + p3[index] - p0[index] for index in range(3))
    planarity = abs(sum(p2_delta[index] * normal[index] for index in range(3)))
    closure = math.sqrt(sum((p2[index] - expected_p2[index]) ** 2 for index in range(3)))
    z_values = [position[2] for position in positions]
    reference_quaternion = surface_input.corners[0].orientation_xyzw
    quaternion_dots = [abs(sum(reference_quaternion[i] * corner.orientation_xyzw[i] for i in range(4))) for corner in surface_input.corners]
    orientation_spread = 2.0 * math.acos(min(1.0, min(quaternion_dots)))
    planar = (
        abs(cosine) <= 0.02
        and planarity <= 1e-4
        and closure <= 2e-4
        and orientation_spread <= math.radians(5.0)
    )
    return SurfaceInputAssessment(
        width_m=width,
        height_m=height,
        orthogonality_cosine=cosine,
        planarity_residual_m=planarity,
        rectangle_closure_error_m=closure,
        z_range_m=max(z_values) - min(z_values),
        orientation_spread_rad=orientation_spread,
        planar_contract_eligible=planar,
    )


def to_planar_surface_calibration(surface_input: SurfaceInputSet) -> SurfaceCalibration:
    """Build the existing planar backend only after the geometry gate passes."""

    assessment = assess_surface_input(surface_input)
    if not assessment.planar_contract_eligible:
        raise ValueError(
            "four-corner evidence is not eligible for planar SurfaceCalibration: "
            f"planarity_residual_m={assessment.planarity_residual_m:.9g}, "
            f"rectangle_closure_error_m={assessment.rectangle_closure_error_m:.9g}"
        )
    return calibrate_surface(surface_input.corners, safe_inset_m=0.003)
