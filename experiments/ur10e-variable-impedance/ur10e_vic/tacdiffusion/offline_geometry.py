"""Hash-bound, no-motion TacDiffusion tube reference primitives."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Sequence


REFERENCE_SCHEMA = "ur10e_tacdiffusion_offline_geometry_reference/v1"
LINE_FAMILY = "line_out_and_back_4s"
CIRCLE_FAMILY = "full_circle_radius_0_5mm_8s"
ANCHOR_FAMILY = "anchor_circle_2s"
REFERENCE_RATE_HZ = 500
RADIUS_M = 0.0005


def _smoothstep(x: float) -> tuple[float, float, float]:
    """Return quintic smoothstep value and first two time-normalized derivatives."""

    x = max(0.0, min(1.0, x))
    value = 10.0 * x**3 - 15.0 * x**4 + 6.0 * x**5
    first = 30.0 * x**2 - 60.0 * x**3 + 30.0 * x**4
    second = 60.0 * x - 180.0 * x**2 + 120.0 * x**3
    return value, first, second


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GeometrySample:
    timestamp_s: float
    u_offset_m: float
    v_offset_m: float
    du_dt_m_s: float
    dv_dt_m_s: float
    ddu_dt2_m_s2: float
    ddv_dt2_m_s2: float
    pose_base: tuple[float, ...]

    def __post_init__(self) -> None:
        values = (
            self.timestamp_s,
            self.u_offset_m,
            self.v_offset_m,
            self.du_dt_m_s,
            self.dv_dt_m_s,
            self.ddu_dt2_m_s2,
            self.ddv_dt2_m_s2,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("geometry sample contains non-finite values")
        pose = tuple(float(value) for value in self.pose_base)
        if len(pose) != 6 or not all(math.isfinite(value) for value in pose):
            raise ValueError("geometry pose must contain six finite values")
        object.__setattr__(self, "pose_base", pose)

    @property
    def speed_m_s(self) -> float:
        return math.hypot(self.du_dt_m_s, self.dv_dt_m_s)

    @property
    def acceleration_m_s2(self) -> float:
        return math.hypot(self.ddu_dt2_m_s2, self.ddv_dt2_m_s2)

    def as_json(self) -> dict[str, Any]:
        return {
            "timestamp_s": self.timestamp_s,
            "u_offset_m": self.u_offset_m,
            "v_offset_m": self.v_offset_m,
            "du_dt_m_s": self.du_dt_m_s,
            "dv_dt_m_s": self.dv_dt_m_s,
            "ddu_dt2_m_s2": self.ddu_dt2_m_s2,
            "ddv_dt2_m_s2": self.ddv_dt2_m_s2,
            "pose_base": list(self.pose_base),
        }


@dataclass(frozen=True)
class OfflineGeometryReference:
    family: str
    duration_s: float
    rate_hz: int
    radius_m: float | None
    fixed_orientation: tuple[float, ...]
    surface_manifest_sha256: str
    samples: tuple[GeometrySample, ...]
    numeric_sanity: dict[str, Any]
    geometry_sha256: str

    @property
    def max_speed_m_s(self) -> float:
        return max((sample.speed_m_s for sample in self.samples), default=0.0)

    @property
    def max_acceleration_m_s2(self) -> float:
        return max((sample.acceleration_m_s2 for sample in self.samples), default=0.0)

    def as_json(self) -> dict[str, Any]:
        return {
            "schema": REFERENCE_SCHEMA,
            "family": self.family,
            "duration_s": self.duration_s,
            "rate_hz": self.rate_hz,
            "radius_m": self.radius_m,
            "fixed_orientation": list(self.fixed_orientation),
            "surface_manifest_sha256": self.surface_manifest_sha256,
            "numeric_sanity": self.numeric_sanity,
            "geometry_sha256": self.geometry_sha256,
            "row_count": len(self.samples),
            "samples": [sample.as_json() for sample in self.samples],
            "live_actions": False,
            "motion_enabled": False,
        }


def _pose_for_offset(tube: Any, anchor_pose: Sequence[float], u_offset: float, v_offset: float) -> tuple[float, ...]:
    anchor = tuple(float(value) for value in anchor_pose)
    if len(anchor) != 6 or not all(math.isfinite(value) for value in anchor):
        raise ValueError("anchor pose must contain six finite values")
    return (
        anchor[0] + tube.u_axis_base[0] * u_offset + tube.v_axis_base[0] * v_offset,
        anchor[1] + tube.u_axis_base[1] * u_offset + tube.v_axis_base[1] * v_offset,
        anchor[2] + tube.u_axis_base[2] * u_offset + tube.v_axis_base[2] * v_offset,
        anchor[3],
        anchor[4],
        anchor[5],
    )


def _line_offset(timestamp_s: float, duration_s: float, radius_m: float) -> tuple[float, float, float, float, float, float]:
    if timestamp_s <= 1.0:
        start, end, segment_start, segment_duration = 0.0, radius_m, 0.0, 1.0
    elif timestamp_s <= 3.0:
        start, end, segment_start, segment_duration = radius_m, -radius_m, 1.0, 2.0
    else:
        start, end, segment_start, segment_duration = -radius_m, 0.0, 3.0, 1.0
    x = (timestamp_s - segment_start) / segment_duration
    smooth, smooth_d, smooth_dd = _smoothstep(x)
    delta = end - start
    return (
        start + delta * smooth,
        0.0,
        delta * smooth_d / segment_duration,
        0.0,
        delta * smooth_dd / segment_duration**2,
        0.0,
    )


def _circle_offset(timestamp_s: float, duration_s: float, radius_m: float) -> tuple[float, float, float, float, float, float]:
    phase, phase_d, phase_dd = _smoothstep(timestamp_s / duration_s)
    omega = 2.0 * math.pi
    theta = omega * phase
    theta_d = omega * phase_d / duration_s
    theta_dd = omega * phase_dd / duration_s**2
    c, s = math.cos(theta), math.sin(theta)
    return (
        radius_m * c,
        radius_m * s,
        -radius_m * s * theta_d,
        radius_m * c * theta_d,
        -radius_m * (c * theta_d**2 + s * theta_dd),
        radius_m * (-s * theta_d**2 + c * theta_dd),
    )


def _anchor_circle_offset(
    timestamp_s: float,
    duration_s: float,
    radius_m: float,
) -> tuple[float, float, float, float, float, float]:
    values = _circle_offset(timestamp_s, duration_s, radius_m)
    # The retained anchor-circle diagnostic starts and ends at the fresh
    # anchor pose, matching the existing 2 s plumbing reference.
    return (values[0] - radius_m, values[1], values[2], values[3], values[4], values[5])


def _make_reference(
    tube: Any,
    anchor_pose: Sequence[float],
    *,
    family: str,
    duration_s: float,
    radius_m: float,
    rate_hz: int,
    offset_fn: Any,
) -> OfflineGeometryReference:
    if rate_hz != REFERENCE_RATE_HZ:
        raise ValueError("offline geometry rate is frozen at 500 Hz")
    if duration_s <= 0.0 or not math.isfinite(duration_s):
        raise ValueError("geometry duration must be finite and positive")
    if radius_m <= 0.0 or not math.isfinite(radius_m):
        raise ValueError("geometry radius must be finite and positive")
    anchor = tuple(float(value) for value in anchor_pose)
    tube.assert_contains(anchor, role="anchor")
    count = int(round(duration_s * rate_hz)) + 1
    samples: list[GeometrySample] = []
    for index in range(count):
        timestamp = min(duration_s, index / rate_hz)
        u, v, du, dv, ddu, ddv = offset_fn(timestamp, duration_s, radius_m)
        pose = _pose_for_offset(tube, anchor, u, v)
        tube.assert_contains(pose, role="desired")
        samples.append(
            GeometrySample(
                timestamp,
                u,
                v,
                du,
                dv,
                ddu,
                ddv,
                pose,
            )
        )
    payload = {
        "schema": REFERENCE_SCHEMA,
        "family": family,
        "duration_s": duration_s,
        "rate_hz": rate_hz,
        "radius_m": radius_m,
        "fixed_orientation": list(anchor[3:]),
        "surface_manifest_sha256": tube.surface_manifest_sha256,
        "samples": [sample.as_json() for sample in samples],
    }
    geometry_sha256 = _canonical_hash(payload)
    numeric_sanity = {
        "path_span_m": max(
            math.hypot(sample.u_offset_m, sample.v_offset_m) for sample in samples
        ),
        "max_speed_m_s": max(sample.speed_m_s for sample in samples),
        "max_acceleration_m_s2": max(sample.acceleration_m_s2 for sample in samples),
        "fixed_orientation": True,
        "tube_rejects_one_um_outside": _one_um_outside_rejects(tube, anchor),
        "motion_enabled": False,
    }
    return OfflineGeometryReference(
        family=family,
        duration_s=duration_s,
        rate_hz=rate_hz,
        radius_m=radius_m,
        fixed_orientation=anchor[3:],
        surface_manifest_sha256=tube.surface_manifest_sha256,
        samples=tuple(samples),
        numeric_sanity=numeric_sanity,
        geometry_sha256=geometry_sha256,
    )


def _one_um_outside_rejects(tube: Any, anchor_pose: Sequence[float]) -> bool:
    candidate = (
        tube.center_base_m[0]
        + tube.u_axis_base[0] * (tube.safe_u_half_width_m + 1e-6),
        tube.center_base_m[1]
        + tube.u_axis_base[1] * (tube.safe_u_half_width_m + 1e-6),
        tube.center_base_m[2]
        + tube.u_axis_base[2] * (tube.safe_u_half_width_m + 1e-6),
        float(anchor_pose[3]),
        float(anchor_pose[4]),
        float(anchor_pose[5]),
    )
    try:
        tube.assert_contains(candidate, role="numeric_sanity")
    except (RuntimeError, ValueError):
        return True
    return False


def line_out_and_back_4s(
    tube: Any,
    anchor_pose: Sequence[float],
    *,
    rate_hz: int = REFERENCE_RATE_HZ,
    radius_m: float = RADIUS_M,
) -> OfflineGeometryReference:
    """Generate ``0 -> +0.5 mm -> -0.5 mm -> 0`` with fixed orientation."""

    if not math.isclose(radius_m, RADIUS_M, rel_tol=0.0, abs_tol=1e-15):
        raise ValueError("line reference radius is frozen at 0.5 mm")
    return _make_reference(
        tube,
        anchor_pose,
        family=LINE_FAMILY,
        duration_s=4.0,
        radius_m=radius_m,
        rate_hz=rate_hz,
        offset_fn=_line_offset,
    )


def full_circle_radius_0_5mm_8s(
    tube: Any,
    anchor_pose: Sequence[float],
    *,
    rate_hz: int = REFERENCE_RATE_HZ,
    radius_m: float = RADIUS_M,
) -> OfflineGeometryReference:
    """Generate one smooth full circle of radius 0.5 mm with fixed orientation."""

    if not math.isclose(radius_m, RADIUS_M, rel_tol=0.0, abs_tol=1e-15):
        raise ValueError("circle reference radius is frozen at 0.5 mm")
    return _make_reference(
        tube,
        anchor_pose,
        family=CIRCLE_FAMILY,
        duration_s=8.0,
        radius_m=radius_m,
        rate_hz=rate_hz,
        offset_fn=_anchor_circle_offset,
    )


def anchor_circle_2s(
    tube: Any,
    anchor_pose: Sequence[float],
    *,
    rate_hz: int = REFERENCE_RATE_HZ,
    radius_m: float = 0.001,
) -> OfflineGeometryReference:
    """Retain the existing 2 s anchor-circle plumbing diagnostic."""

    return _make_reference(
        tube,
        anchor_pose,
        family=ANCHOR_FAMILY,
        duration_s=2.0,
        radius_m=radius_m,
        rate_hz=rate_hz,
        offset_fn=_anchor_circle_offset,
    )


def build_offline_reference_set(
    tube: Any,
    anchor_pose: Sequence[float],
    *,
    rate_hz: int = REFERENCE_RATE_HZ,
) -> dict[str, OfflineGeometryReference]:
    return {
        ANCHOR_FAMILY: anchor_circle_2s(tube, anchor_pose, rate_hz=rate_hz),
        LINE_FAMILY: line_out_and_back_4s(tube, anchor_pose, rate_hz=rate_hz),
        CIRCLE_FAMILY: full_circle_radius_0_5mm_8s(tube, anchor_pose, rate_hz=rate_hz),
    }


__all__ = [
    "ANCHOR_FAMILY",
    "CIRCLE_FAMILY",
    "GeometrySample",
    "LINE_FAMILY",
    "OfflineGeometryReference",
    "REFERENCE_RATE_HZ",
    "REFERENCE_SCHEMA",
    "RADIUS_M",
    "anchor_circle_2s",
    "build_offline_reference_set",
    "full_circle_radius_0_5mm_8s",
    "line_out_and_back_4s",
]
