"""Unknown-surface nominal references and a hard TCP tube guard.

The live reference deliberately consumes only the taught XY footprint.  The
captured corner Z values and the offline CAD registration are diagnostics and
never become a live height or surface-normal feed-forward source.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Mapping, Sequence

from .surface import SurfaceCalibration, SurfacePose, calibrate_surface
from .trajectory import (
    BoundedTrajectory,
    TrajectoryProfile,
    TrajectorySample,
    generate_bounded_trajectory,
)


SURFACE_INPUT_SCHEMA = "ur10e_tacdiffusion_surface_input/v1"
UNKNOWN_SURFACE_REFERENCE_SCHEMA = "ur10e_tacdiffusion_unknown_surface_reference/v1"


def _finite(values: Sequence[float], length: int, name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) != length or not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} must contain {length} finite values")
    return result


def _unit_xy(values: Sequence[float], name: str) -> tuple[float, float]:
    x, y = _finite(values, 2, name)
    norm = math.hypot(x, y)
    if norm <= 1e-9:
        raise ValueError(f"{name} must be non-zero")
    return (x / norm, y / norm)


def _dot_xy(left: Sequence[float], right: Sequence[float]) -> float:
    return float(left[0]) * float(right[0]) + float(left[1]) * float(right[1])


def _rotvec_to_quaternion(rotvec: Sequence[float]) -> tuple[float, float, float, float]:
    rx, ry, rz = _finite(rotvec, 3, "anchor orientation")
    angle = math.sqrt(rx * rx + ry * ry + rz * rz)
    if angle <= 1e-12:
        return (0.0, 0.0, 0.0, 1.0)
    scale = math.sin(angle / 2.0) / angle
    return (rx * scale, ry * scale, rz * scale, math.cos(angle / 2.0))


def rotation_vector_distance_rad(
    left: Sequence[float],
    right: Sequence[float],
) -> float:
    """Return the shortest SO(3) distance, including equivalent ±π forms."""

    left_q = _rotvec_to_quaternion(left)
    right_q = _rotvec_to_quaternion(right)
    quaternion_dot = sum(a * b for a, b in zip(left_q, right_q))
    return 2.0 * math.acos(min(1.0, max(0.0, abs(quaternion_dot))))


@dataclass(frozen=True)
class UnknownSurfaceTube:
    """A constant-height nominal plane inside the taught XY footprint."""

    frame_id: str
    center_base_m: tuple[float, float, float]
    anchor_position_base_m: tuple[float, float, float]
    u_axis_base: tuple[float, float, float]
    v_axis_base: tuple[float, float, float]
    u_half_width_m: float
    v_half_width_m: float
    safe_inset_m: float
    anchor_orientation_rotvec: tuple[float, float, float]
    normal_half_width_m: float
    orientation_tolerance_rad: float
    surface_manifest_sha256: str

    def __post_init__(self) -> None:
        if self.frame_id != "base":
            raise ValueError("unknown-surface tube requires the base frame")
        for name in ("u_half_width_m", "v_half_width_m", "normal_half_width_m"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and positive")
        if (
            not math.isfinite(self.safe_inset_m)
            or self.safe_inset_m < 0.0
            or self.u_half_width_m <= self.safe_inset_m
            or self.v_half_width_m <= self.safe_inset_m
        ):
            raise ValueError("safe inset leaves no tube interior")
        if not math.isfinite(self.orientation_tolerance_rad) or self.orientation_tolerance_rad <= 0.0:
            raise ValueError("orientation tolerance must be finite and positive")
        if len(self.surface_manifest_sha256) != 64:
            raise ValueError("surface manifest SHA-256 is invalid")

    @property
    def reaction_normal_base(self) -> tuple[float, float, float]:
        return (0.0, 0.0, 1.0)

    @property
    def approach_normal_base(self) -> tuple[float, float, float]:
        return (0.0, 0.0, -1.0)

    @property
    def safe_u_half_width_m(self) -> float:
        return self.u_half_width_m - self.safe_inset_m

    @property
    def safe_v_half_width_m(self) -> float:
        return self.v_half_width_m - self.safe_inset_m

    def local_coordinates(self, pose_base: Sequence[float]) -> tuple[float, float, float]:
        pose = _finite(pose_base, 6, "TCP pose")
        delta = (
            pose[0] - self.center_base_m[0],
            pose[1] - self.center_base_m[1],
            pose[2] - self.center_base_m[2],
        )
        return (
            sum(delta[index] * self.u_axis_base[index] for index in range(3)),
            sum(delta[index] * self.v_axis_base[index] for index in range(3)),
            delta[2],
        )

    def assert_contains(self, pose_base: Sequence[float], *, role: str) -> None:
        pose = _finite(pose_base, 6, f"{role} TCP pose")
        u_m, v_m, normal_m = self.local_coordinates(pose)
        if abs(u_m) > self.safe_u_half_width_m + 1e-9:
            raise RuntimeError(f"{role}_tcp_tube_u_guard")
        if abs(v_m) > self.safe_v_half_width_m + 1e-9:
            raise RuntimeError(f"{role}_tcp_tube_v_guard")
        if abs(normal_m) > self.normal_half_width_m + 1e-9:
            raise RuntimeError(f"{role}_tcp_tube_normal_guard")
        orientation_error = rotation_vector_distance_rad(
            pose[3:],
            self.anchor_orientation_rotvec,
        )
        if orientation_error > self.orientation_tolerance_rad + 1e-9:
            raise RuntimeError(f"{role}_tcp_tube_orientation_guard")

    def assert_actual_and_desired(
        self,
        actual_pose_base: Sequence[float],
        desired_pose_base: Sequence[float],
    ) -> None:
        self.assert_contains(actual_pose_base, role="actual")
        self.assert_contains(desired_pose_base, role="desired")

    def planar_calibration(self) -> SurfaceCalibration:
        """Return a synthetic constant-Z plane for nominal path generation."""

        q = _rotvec_to_quaternion(self.anchor_orientation_rotvec)

        def point(u_m: float, v_m: float) -> SurfacePose:
            return SurfacePose(
                (
                    self.center_base_m[0] + self.u_axis_base[0] * u_m + self.v_axis_base[0] * v_m,
                    self.center_base_m[1] + self.u_axis_base[1] * u_m + self.v_axis_base[1] * v_m,
                    self.center_base_m[2],
                ),
                q,
                self.frame_id,
            )

        corners = (
            point(-self.u_half_width_m, -self.v_half_width_m),
            point(self.u_half_width_m, -self.v_half_width_m),
            point(self.u_half_width_m, self.v_half_width_m),
            point(-self.u_half_width_m, self.v_half_width_m),
        )
        return calibrate_surface(
            corners,
            safe_inset_m=self.safe_inset_m,
            planarity_tolerance_m=1e-9,
            rectangle_tolerance_m=1e-9,
            orientation_tolerance_rad=self.orientation_tolerance_rad,
        )


def load_unknown_surface_tube(
    manifest_path: str | Path,
    *,
    anchor_pose_base: Sequence[float],
    normal_half_width_m: float,
) -> UnknownSurfaceTube:
    """Bind a tube to a fresh episode-start pose without consuming CAD Z."""

    path = Path(manifest_path)
    raw = path.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, Mapping) or payload.get("schema") != SURFACE_INPUT_SCHEMA:
        raise ValueError("unknown-surface manifest schema mismatch")
    convention = payload.get("coordinate_convention")
    if not isinstance(convention, Mapping) or convention.get("frame_id") != "base":
        raise ValueError("unknown-surface manifest frame mismatch")
    corners = payload.get("corners")
    if not isinstance(corners, list) or len(corners) != 4:
        raise ValueError("unknown-surface manifest requires four corners")
    expected_labels = ("right_upper", "left_upper", "left_lower", "right_lower")
    points: list[tuple[float, ...]] = []
    for expected, row in zip(expected_labels, corners):
        if not isinstance(row, Mapping) or row.get("user_label") != expected:
            raise ValueError("unknown-surface corner order mismatch")
        points.append(_finite(row.get("tcp_pose", ()), 6, f"{expected} pose"))

    anchor = _finite(anchor_pose_base, 6, "anchor pose")
    p0, p1, _, p3 = points
    u_xy = _unit_xy((p1[0] - p0[0], p1[1] - p0[1]), "taught u axis")
    v_seed = _unit_xy((p3[0] - p0[0], p3[1] - p0[1]), "taught v axis")
    v_projected = (
        v_seed[0] - _dot_xy(v_seed, u_xy) * u_xy[0],
        v_seed[1] - _dot_xy(v_seed, u_xy) * u_xy[1],
    )
    v_xy = _unit_xy(v_projected, "orthogonal taught v axis")
    if u_xy[0] * v_xy[1] - u_xy[1] * v_xy[0] < 0.0:
        v_xy = (-v_xy[0], -v_xy[1])

    centroid_xy = (
        sum(point[0] for point in points) / 4.0,
        sum(point[1] for point in points) / 4.0,
    )
    projected_u = [
        (point[0] - centroid_xy[0]) * u_xy[0] + (point[1] - centroid_xy[1]) * u_xy[1]
        for point in points
    ]
    projected_v = [
        (point[0] - centroid_xy[0]) * v_xy[0] + (point[1] - centroid_xy[1]) * v_xy[1]
        for point in points
    ]
    u_mid = (min(projected_u) + max(projected_u)) / 2.0
    v_mid = (min(projected_v) + max(projected_v)) / 2.0
    center_xy = (
        centroid_xy[0] + u_xy[0] * u_mid + v_xy[0] * v_mid,
        centroid_xy[1] + u_xy[1] * u_mid + v_xy[1] * v_mid,
    )
    gates = payload.get("preprocessing_and_gates")
    planar_gate = gates.get("planar_surface_gate") if isinstance(gates, Mapping) else None
    if not isinstance(planar_gate, Mapping):
        raise ValueError("unknown-surface safe-inset gate missing")
    safe_inset_m = float(planar_gate.get("safe_inset_m", math.nan))
    orientation_tolerance_rad = float(
        planar_gate.get("orientation_tolerance_rad", math.nan)
    )
    return UnknownSurfaceTube(
        frame_id="base",
        center_base_m=(center_xy[0], center_xy[1], anchor[2]),
        anchor_position_base_m=tuple(anchor[:3]),
        u_axis_base=(u_xy[0], u_xy[1], 0.0),
        v_axis_base=(v_xy[0], v_xy[1], 0.0),
        u_half_width_m=(max(projected_u) - min(projected_u)) / 2.0,
        v_half_width_m=(max(projected_v) - min(projected_v)) / 2.0,
        safe_inset_m=safe_inset_m,
        anchor_orientation_rotvec=tuple(anchor[3:6]),
        normal_half_width_m=float(normal_half_width_m),
        orientation_tolerance_rad=orientation_tolerance_rad,
        surface_manifest_sha256=hashlib.sha256(raw).hexdigest(),
    )


def generate_unknown_surface_references(
    tube: UnknownSurfaceTube,
    *,
    family: str,
    seed: int,
    duration_s: float,
    capture_duration_s: float | None = None,
    canary_radius_m: float | None = None,
    rate_hz: int = 500,
    profile: TrajectoryProfile | None = None,
) -> tuple[BoundedTrajectory, tuple[dict[str, object], ...]]:
    if capture_duration_s is not None and (
        not math.isfinite(capture_duration_s)
        or capture_duration_s <= 0.0
        or capture_duration_s > duration_s
    ):
        raise ValueError("capture_duration_s must be in (0, duration_s]")
    surface = tube.planar_calibration()
    if family == "anchor_circle":
        if canary_radius_m is None or not math.isfinite(canary_radius_m) or canary_radius_m <= 0.0:
            raise ValueError("anchor_circle requires a positive canary_radius_m")
        selected_profile = profile or TrajectoryProfile(
            speed_scale=1.0,
            normal_force_target_n=0.0,
            preload_n=0.0,
        )
        anchor_pose = (
            *tube.anchor_position_base_m,
            *tube.anchor_orientation_rotvec,
        )
        tube.assert_contains(anchor_pose, role="anchor")
        anchor_u, anchor_v, _ = tube.local_coordinates(anchor_pose)
        center_u = anchor_u - canary_radius_m
        if (
            center_u - canary_radius_m < -tube.safe_u_half_width_m
            or center_u + canary_radius_m > tube.safe_u_half_width_m
            or anchor_v - canary_radius_m < -tube.safe_v_half_width_m
            or anchor_v + canary_radius_m > tube.safe_v_half_width_m
        ):
            raise ValueError("anchor_circle does not fit inside the hard tube")
        count = int(round(duration_s * rate_hz)) + 1
        samples: list[TrajectorySample] = []
        for index in range(count):
            timestamp = min(duration_s, index / rate_hz)
            phase = timestamp / duration_s
            smooth = 10.0 * phase**3 - 15.0 * phase**4 + 6.0 * phase**5
            smooth_d = (
                30.0 * phase**2 - 60.0 * phase**3 + 30.0 * phase**4
            ) / duration_s
            smooth_dd = (
                60.0 * phase - 180.0 * phase**2 + 120.0 * phase**3
            ) / (duration_s * duration_s)
            theta = 2.0 * math.pi * smooth
            theta_d = 2.0 * math.pi * smooth_d
            theta_dd = 2.0 * math.pi * smooth_dd
            u_m = center_u + canary_radius_m * math.cos(theta)
            v_m = anchor_v + canary_radius_m * math.sin(theta)
            du_m = -canary_radius_m * math.sin(theta) * theta_d
            dv_m = canary_radius_m * math.cos(theta) * theta_d
            ddu_m = -canary_radius_m * (
                math.cos(theta) * theta_d * theta_d
                + math.sin(theta) * theta_dd
            )
            ddv_m = canary_radius_m * (
                -math.sin(theta) * theta_d * theta_d
                + math.cos(theta) * theta_dd
            )
            speed = math.hypot(du_m, dv_m)
            acceleration = math.hypot(ddu_m, ddv_m)
            curvature = (
                abs(du_m * ddv_m - dv_m * ddu_m) / speed**3
                if speed > 1e-12
                else 0.0
            )
            if speed > 0.04 + 1e-9 or acceleration > 0.8 + 1e-9:
                raise ValueError("anchor_circle dynamics exceed hard bounds")
            samples.append(
                TrajectorySample(
                    timestamp,
                    u_m / tube.safe_u_half_width_m,
                    v_m / tube.safe_v_half_width_m,
                    du_m / tube.safe_u_half_width_m,
                    dv_m / tube.safe_v_half_width_m,
                    ddu_m / tube.safe_u_half_width_m,
                    ddv_m / tube.safe_v_half_width_m,
                    speed,
                    acceleration,
                    curvature,
                    selected_profile.normal_force_target_n,
                    selected_profile.preload_n,
                )
            )
        trajectory = BoundedTrajectory(
            "ur10e_tacdiffusion_trajectory/v1",
            family,
            int(seed),
            float(duration_s),
            int(rate_hz),
            selected_profile,
            tuple(samples),
        )
    else:
        trajectory = generate_bounded_trajectory(
            surface,
            family=family,
            seed=seed,
            duration_s=duration_s,
            rate_hz=rate_hz,
            profile=profile,
        )
    rows: list[dict[str, object]] = []
    capture_samples = (
        len(trajectory.samples)
        if capture_duration_s is None
        else int(round(capture_duration_s * rate_hz)) + 1
    )
    for index in range(capture_samples):
        reference = trajectory.episode_reference(surface, index).as_mapping()
        reference["schema"] = UNKNOWN_SURFACE_REFERENCE_SCHEMA
        reference["surface_manifest_sha256"] = tube.surface_manifest_sha256
        reference["height_source"] = "fresh_episode_anchor_constant_z"
        reference["normal_source"] = "nominal_task_loading_axis_base_positive_z"
        tube.assert_contains(reference["desired_pose_base"], role="desired")
        rows.append(reference)
    return trajectory, tuple(rows)
