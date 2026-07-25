"""Calibrated, bounded local surface geometry for TacDiffusion episodes.

The calibration is deliberately geometry-only.  It does not read a robot,
write a controller, or authorize contact.  Corner order is part of the
contract: ``[u-,v-], [u+,v-], [u+,v+], [u-,v+]`` as seen from the supplied
frame.  The fitted reaction and approach normals retain the UR contact-frame
semantics instead of hiding a sign in a path generator.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Sequence


def _finite_vector(values: Iterable[float], size: int, name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) != size or not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} must contain {size} finite values")
    return result


def _norm(vector: Sequence[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


def _unit(vector: Sequence[float], name: str) -> tuple[float, float, float]:
    length = _norm(vector)
    if length <= 1e-12:
        raise ValueError(f"{name} must be non-zero")
    return tuple(float(value) / length for value in vector)  # type: ignore[return-value]


def _sub(left: Sequence[float], right: Sequence[float]) -> tuple[float, float, float]:
    return tuple(a - b for a, b in zip(left, right))  # type: ignore[return-value]


def _add(*vectors: Sequence[float]) -> tuple[float, float, float]:
    return tuple(sum(vector[index] for vector in vectors) for index in range(3))  # type: ignore[return-value]


def _scale(vector: Sequence[float], scalar: float) -> tuple[float, float, float]:
    return tuple(float(scalar) * value for value in vector)  # type: ignore[return-value]


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def _cross(left: Sequence[float], right: Sequence[float]) -> tuple[float, float, float]:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


@dataclass(frozen=True)
class SurfacePose:
    position_base_m: tuple[float, ...] | Sequence[float]
    orientation_xyzw: tuple[float, ...] | Sequence[float]
    frame_id: str = "base"

    def __post_init__(self) -> None:
        object.__setattr__(self, "position_base_m", _finite_vector(self.position_base_m, 3, "position_base_m"))
        quaternion = _finite_vector(self.orientation_xyzw, 4, "orientation_xyzw")
        length = _norm(quaternion)
        if length <= 1e-12:
            raise ValueError("orientation_xyzw must be non-zero")
        object.__setattr__(self, "orientation_xyzw", tuple(value / length for value in quaternion))
        if not str(self.frame_id).strip():
            raise ValueError("frame_id must be non-empty")


@dataclass(frozen=True)
class SurfaceCalibration:
    schema_version: str
    frame_id: str
    corners: tuple[SurfacePose, ...]
    center_base_m: tuple[float, float, float]
    u_axis_base: tuple[float, float, float]
    v_axis_base: tuple[float, float, float]
    reaction_normal_base: tuple[float, float, float]
    approach_normal_base: tuple[float, float, float]
    width_m: float
    height_m: float
    safe_inset_m: float
    planarity_residual_m: float
    orientation_spread_rad: float

    @property
    def safe_u_bounds_m(self) -> tuple[float, float]:
        half = self.width_m / 2.0 - self.safe_inset_m
        return (-half, half)

    @property
    def safe_v_bounds_m(self) -> tuple[float, float]:
        half = self.height_m / 2.0 - self.safe_inset_m
        return (-half, half)

    def local_to_base(self, u_m: float, v_m: float, normal_offset_m: float = 0.0) -> tuple[float, float, float]:
        if not all(math.isfinite(value) for value in (u_m, v_m, normal_offset_m)):
            raise ValueError("surface coordinates must be finite")
        u_min, u_max = self.safe_u_bounds_m
        v_min, v_max = self.safe_v_bounds_m
        if not (u_min - 1e-12 <= u_m <= u_max + 1e-12):
            raise ValueError("u coordinate exceeds safe inset")
        if not (v_min - 1e-12 <= v_m <= v_max + 1e-12):
            raise ValueError("v coordinate exceeds safe inset")
        return _add(
            self.center_base_m,
            _scale(self.u_axis_base, u_m),
            _scale(self.v_axis_base, v_m),
            _scale(self.approach_normal_base, normal_offset_m),
        )

    def normalized_to_base(self, u_normalized: float, v_normalized: float, normal_offset_m: float = 0.0) -> tuple[float, float, float]:
        if not (-1.0 - 1e-12 <= u_normalized <= 1.0 + 1e-12):
            raise ValueError("normalized u coordinate exceeds safe inset bounds [-1, 1]")
        if not (-1.0 - 1e-12 <= v_normalized <= 1.0 + 1e-12):
            raise ValueError("normalized v coordinate exceeds safe inset bounds [-1, 1]")
        return self.local_to_base(
            u_normalized * (self.width_m / 2.0 - self.safe_inset_m),
            v_normalized * (self.height_m / 2.0 - self.safe_inset_m),
            normal_offset_m,
        )


def calibrate_surface(
    corners: Sequence[SurfacePose],
    *,
    safe_inset_m: float = 0.003,
    planarity_tolerance_m: float = 1e-4,
    rectangle_tolerance_m: float = 2e-4,
    orientation_tolerance_rad: float = math.radians(5.0),
) -> SurfaceCalibration:
    """Fit and validate a four-corner rectangular contact surface."""

    if len(corners) != 4:
        raise ValueError("surface calibration requires exactly four ordered corners")
    if safe_inset_m < 0.0 or not math.isfinite(safe_inset_m):
        raise ValueError("safe_inset_m must be finite and non-negative")
    if planarity_tolerance_m <= 0.0 or rectangle_tolerance_m <= 0.0:
        raise ValueError("surface tolerances must be positive")
    frame_id = corners[0].frame_id
    if any(corner.frame_id != frame_id for corner in corners):
        raise ValueError("all surface corners must use the same frame")
    positions = [corner.position_base_m for corner in corners]
    p0, p1, p2, p3 = positions
    edge_u = _sub(p1, p0)
    edge_v = _sub(p3, p0)
    width = _norm(edge_u)
    height = _norm(edge_v)
    if width <= 2.0 * safe_inset_m or height <= 2.0 * safe_inset_m:
        raise ValueError("surface dimensions are insufficient for the requested safe inset")
    u_axis = _unit(edge_u, "u edge")
    v_raw = _unit(edge_v, "v edge")
    if abs(_dot(u_axis, v_raw)) > 0.02:
        raise ValueError("surface corners are not rectangular: adjacent edges are not orthogonal")
    v_axis = _unit(_sub(v_raw, _scale(u_axis, _dot(v_raw, u_axis))), "orthogonal v edge")
    reaction_normal = _unit(_cross(u_axis, v_axis), "surface normal")
    planarity_residual = abs(_dot(_sub(p2, p0), reaction_normal))
    if planarity_residual > planarity_tolerance_m:
        raise ValueError("surface corners are not coplanar")
    expected_p2 = _add(p1, p3, _scale(p0, -1.0))
    if _norm(_sub(p2, expected_p2)) > rectangle_tolerance_m:
        raise ValueError("surface corners do not define a rectangle in the supplied order")
    opposite_u = _norm(_sub(p2, p3))
    opposite_v = _norm(_sub(p2, p1))
    if abs(opposite_u - width) > rectangle_tolerance_m or abs(opposite_v - height) > rectangle_tolerance_m:
        raise ValueError("surface opposite edges are inconsistent")
    reference_quaternion = corners[0].orientation_xyzw
    quaternion_dots = [abs(_dot(reference_quaternion, corner.orientation_xyzw)) for corner in corners]
    orientation_spread = 2.0 * math.acos(min(1.0, min(quaternion_dots)))
    if orientation_spread > orientation_tolerance_rad:
        raise ValueError("surface corner orientations are inconsistent")
    center = _scale(_add(p0, p1, p2, p3), 0.25)
    return SurfaceCalibration(
        schema_version="ur10e_surface_calibration/v1",
        frame_id=frame_id,
        corners=tuple(corners),
        center_base_m=tuple(center),
        u_axis_base=tuple(u_axis),
        v_axis_base=tuple(v_axis),
        reaction_normal_base=tuple(reaction_normal),
        approach_normal_base=tuple(-value for value in reaction_normal),
        width_m=width,
        height_m=height,
        safe_inset_m=float(safe_inset_m),
        planarity_residual_m=planarity_residual,
        orientation_spread_rad=orientation_spread,
    )
