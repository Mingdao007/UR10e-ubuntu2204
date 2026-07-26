"""Load and assess the offline TacDiffusion surface-input manifest.

The manifest is deliberately separate from ``SurfaceCalibration``.  The
current four-corner evidence is a curved/non-planar surface, so it must be
assessed before a planar trajectory backend is allowed to consume it.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import struct
from typing import Any, Sequence

import numpy as np

from .surface import SurfaceCalibration, SurfacePose, calibrate_surface


EXPECTED_SCHEMA = "ur10e_tacdiffusion_surface_input/v1"
EXPECTED_CORNER_ORDER = ("right_upper", "left_upper", "left_lower", "right_lower")
EXPECTED_MODEL_MAPPING = (
    ("right_upper", "x_min", "z_max"),
    ("left_upper", "x_mid", "z_max"),
    ("left_lower", "x_mid", "z_min"),
    ("right_lower", "x_min", "z_min"),
)
MODEL_HEIGHT_MISMATCH_TOLERANCE_M = 0.001


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


@dataclass(frozen=True)
class SurfaceModelBinding:
    relative_path: str
    path: Path
    sha256: str
    triangle_count: int
    native_units: str
    scale_to_m: float
    height_axis_in_stl: str
    in_plane_axes_in_stl: tuple[str, str]
    model_corner_points_stl_mm: tuple[tuple[float, float, float], ...]
    predicted_delta_z_m: tuple[float, ...]
    global_top_minimum_is_unique: bool


@dataclass(frozen=True)
class RigidRegistration:
    rotation_model_to_base: tuple[tuple[float, float, float], ...]
    translation_base_m: tuple[float, float, float]
    predicted_points_base_m: tuple[tuple[float, float, float], ...]
    per_point_residual_m: tuple[float, ...]
    rms_residual_m: float
    max_residual_m: float


@dataclass(frozen=True)
class SurfaceModelAssessment:
    binding: SurfaceModelBinding
    registration: RigidRegistration
    measured_minus_model_delta_z_m: tuple[float, ...]
    height_delta_max_abs_m: float
    model_match: bool


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


def _resolve_repo_relative_path(manifest_path: Path, relative_path: str) -> Path:
    for root in (manifest_path.parents[3], *manifest_path.parents):
        candidate = root / relative_path
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"model asset is not present below manifest repo: {relative_path}")


def _binary_stl_vertices(path: Path) -> tuple[int, tuple[tuple[float, float, float], ...]]:
    payload = path.read_bytes()
    if len(payload) < 84:
        raise ValueError("selected STL is shorter than a binary STL header")
    triangle_count = struct.unpack_from("<I", payload, 80)[0]
    expected_size = 84 + 50 * triangle_count
    if len(payload) != expected_size:
        raise ValueError("selected STL is not the expected exact binary STL payload")
    vertices: list[tuple[float, float, float]] = []
    for index in range(triangle_count):
        values = struct.unpack_from("<12f", payload, 84 + 50 * index)
        vertices.extend((tuple(values[3:6]), tuple(values[6:9]), tuple(values[9:12])))
    return triangle_count, tuple(vertices)


def _top_surface_corner(
    vertices: Sequence[Sequence[float]], x_target: float, z_target: float
) -> tuple[float, float, float]:
    candidates = [
        vertex
        for vertex in vertices
        if abs(vertex[0] - x_target) <= 1e-4 and abs(vertex[2] - z_target) <= 1e-4
    ]
    if not candidates:
        raise ValueError(f"STL has no surface vertex near x={x_target}, z={z_target}")
    # The v11 binary contains shell thickness.  At a fixed (x,z), the outer
    # top surface is the maximum STL-y member; the lower member is thickness.
    top_y = max(vertex[1] for vertex in candidates)
    return (float(x_target), float(top_y), float(z_target))


def load_surface_model_binding(manifest_path: str | Path) -> SurfaceModelBinding:
    """Hash and deterministically extract the selected v11 outer top surface."""

    path = Path(manifest_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    config = manifest.get("model_binding")
    if not isinstance(config, dict):
        raise ValueError("model_binding is required for the selected surface")
    mapping = tuple(
        (item.get("label"), item.get("x_selector"), item.get("z_selector"))
        for item in config.get("corner_mapping", ())
        if isinstance(item, dict)
    )
    if mapping != EXPECTED_MODEL_MAPPING:
        raise ValueError("model corner correspondence does not match the unique v11 mapping")
    if config.get("native_units") != "mm" or config.get("scale_to_m") != 0.001:
        raise ValueError("selected v11 STL must be bound as millimetres scaled by 0.001")
    if config.get("height_axis_in_stl") != "y" or tuple(config.get("in_plane_axes_in_stl", ())) != ("x", "z"):
        raise ValueError("selected v11 STL axes must be height=y and in-plane=(x,z)")
    relative_path = str(config.get("relative_path", ""))
    stl_path = _resolve_repo_relative_path(path, relative_path)
    actual_sha256 = hashlib.sha256(stl_path.read_bytes()).hexdigest()
    if actual_sha256 != config.get("sha256"):
        raise ValueError("selected v11 STL hash does not match the manifest")
    triangle_count, vertices = _binary_stl_vertices(stl_path)
    x_values = [vertex[0] for vertex in vertices]
    z_values = [vertex[2] for vertex in vertices]
    x_min, x_max = min(x_values), max(x_values)
    z_min, z_max = min(z_values), max(z_values)
    x_mid_target = (x_min + x_max) / 2.0
    x_mid = min(set(x_values), key=lambda value: abs(value - x_mid_target))
    selectors = {"x_min": x_min, "x_mid": x_mid, "z_min": z_min, "z_max": z_max}
    model_points = tuple(
        _top_surface_corner(vertices, selectors[x_selector], selectors[z_selector])
        for _, x_selector, z_selector in EXPECTED_MODEL_MAPPING
    )
    expected_points = tuple(
        tuple(float(value) for value in item["model_point_stl_mm"])
        for item in config.get("corner_mapping", ())
    )
    if model_points != expected_points:
        raise ValueError("selected v11 STL surface corners differ from the manifest")
    top_heights = [point[1] for point in model_points]
    unique_minimum = top_heights[0] == min(top_heights) and sum(
        abs(value - top_heights[0]) <= 1e-6 for value in top_heights
    ) == 1
    if unique_minimum != bool(config.get("global_top_minimum_is_unique")):
        raise ValueError("selected v11 global top minimum uniqueness does not match the manifest")
    predicted_delta_z_m = tuple((point[1] - model_points[0][1]) * 0.001 for point in model_points)
    return SurfaceModelBinding(
        relative_path=relative_path,
        path=stl_path,
        sha256=actual_sha256,
        triangle_count=triangle_count,
        native_units="mm",
        scale_to_m=0.001,
        height_axis_in_stl="y",
        in_plane_axes_in_stl=("x", "z"),
        model_corner_points_stl_mm=model_points,
        predicted_delta_z_m=predicted_delta_z_m,
        global_top_minimum_is_unique=unique_minimum,
    )


def solve_rigid_registration(
    model_points_m: Sequence[Sequence[float]], measured_points_m: Sequence[Sequence[float]]
) -> RigidRegistration:
    """Solve the least-squares model-to-base rigid transform with Kabsch."""

    source = np.asarray(model_points_m, dtype=float)
    target = np.asarray(measured_points_m, dtype=float)
    if source.shape != (4, 3) or target.shape != (4, 3):
        raise ValueError("rigid registration requires four 3D point pairs")
    if not np.isfinite(source).all() or not np.isfinite(target).all():
        raise ValueError("rigid registration points must be finite")
    source_center = source.mean(axis=0)
    target_center = target.mean(axis=0)
    u_matrix, _, v_transpose = np.linalg.svd((source - source_center).T @ (target - target_center))
    rotation = v_transpose.T @ u_matrix.T
    if np.linalg.det(rotation) < 0.0:
        v_transpose[-1, :] *= -1.0
        rotation = v_transpose.T @ u_matrix.T
    translation = target_center - rotation @ source_center
    predicted = (rotation @ source.T).T + translation
    residuals = np.linalg.norm(predicted - target, axis=1)
    return RigidRegistration(
        rotation_model_to_base=tuple(tuple(float(value) for value in row) for row in rotation),
        translation_base_m=tuple(float(value) for value in translation),
        predicted_points_base_m=tuple(tuple(float(value) for value in row) for row in predicted),
        per_point_residual_m=tuple(float(value) for value in residuals),
        rms_residual_m=float(math.sqrt(float(np.mean(residuals**2)))),
        max_residual_m=float(np.max(residuals)),
    )


def assess_surface_model(
    surface_input: SurfaceInputSet,
    binding: SurfaceModelBinding,
    *,
    mismatch_tolerance_m: float = MODEL_HEIGHT_MISMATCH_TOLERANCE_M,
) -> SurfaceModelAssessment:
    """Bind model height deltas and fit the static CAD-to-base transform."""

    if mismatch_tolerance_m <= 0.0 or not math.isfinite(mismatch_tolerance_m):
        raise ValueError("model mismatch tolerance must be finite and positive")
    measured_points = tuple(corner.position_base_m for corner in surface_input.corners)
    measured_delta_z = tuple(point[2] - measured_points[0][2] for point in measured_points)
    height_residuals = tuple(
        measured - predicted for measured, predicted in zip(measured_delta_z, binding.predicted_delta_z_m)
    )
    maximum = max(abs(value) for value in height_residuals)
    registration = solve_rigid_registration(
        tuple(tuple(value * binding.scale_to_m for value in point) for point in binding.model_corner_points_stl_mm),
        measured_points,
    )
    assessment = SurfaceModelAssessment(
        binding=binding,
        registration=registration,
        measured_minus_model_delta_z_m=height_residuals,
        height_delta_max_abs_m=maximum,
        model_match=maximum <= mismatch_tolerance_m,
    )
    if not assessment.model_match:
        raise ValueError(
            "measured height deltas do not match the selected v11 model: "
            f"max_abs_residual_m={maximum:.9g} > tolerance_m={mismatch_tolerance_m:.9g}"
        )
    return assessment


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
