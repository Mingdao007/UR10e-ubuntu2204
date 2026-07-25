"""Seeded, bounded local trajectory references for TacDiffusion episodes."""

from __future__ import annotations

from dataclasses import dataclass
import math
import random
from typing import Callable, Sequence

from .surface import SurfaceCalibration


TRAJECTORY_FAMILIES = (
    "circle",
    "ellipse",
    "figure_eight",
    "lissajous",
    "linear_grid",
    "rounded_arc",
    "seeded_smooth_spline",
)


def _canonical_quaternion_to_rotvec(quaternion: Sequence[float]) -> tuple[float, float, float]:
    """Convert common calibrated xyzw orientation to a canonical rotvec."""

    values = tuple(float(value) for value in quaternion)
    if len(values) != 4 or not all(math.isfinite(value) for value in values):
        raise ValueError("surface orientation quaternion must contain four finite values")
    norm = math.sqrt(sum(value * value for value in values))
    if norm <= 1e-12:
        raise ValueError("surface orientation quaternion must be non-zero")
    x, y, z, w = (value / norm for value in values)
    # q and -q are identical; choose w >= 0 for one deterministic rotvec.
    if w < 0.0:
        x, y, z, w = -x, -y, -z, -w
    w = max(-1.0, min(1.0, w))
    angle = 2.0 * math.acos(w)
    sine_half = math.sqrt(max(0.0, 1.0 - w * w))
    if sine_half <= 1e-10 or angle <= 1e-10:
        return (2.0 * x, 2.0 * y, 2.0 * z)
    scale = angle / sine_half
    return (x * scale, y * scale, z * scale)


@dataclass(frozen=True)
class TrajectoryProfile:
    speed_scale: float
    normal_force_target_n: float
    preload_n: float

    def __post_init__(self) -> None:
        if not all(math.isfinite(value) for value in (self.speed_scale, self.normal_force_target_n, self.preload_n)):
            raise ValueError("trajectory profile must be finite")
        if self.speed_scale <= 0.0 or self.normal_force_target_n < 0.0 or self.preload_n < 0.0:
            raise ValueError("trajectory profile values are outside safe bounds")


@dataclass(frozen=True)
class TrajectorySample:
    timestamp_s: float
    u_normalized: float
    v_normalized: float
    du_dt: float
    dv_dt: float
    ddu_dt2: float
    ddv_dt2: float
    speed_m_s: float
    acceleration_m_s2: float
    curvature_m_inv: float
    normal_force_target_n: float
    preload_n: float

    @property
    def speed_normalized(self) -> float:
        return math.hypot(self.du_dt, self.dv_dt)

    @property
    def acceleration_normalized(self) -> float:
        return math.hypot(self.ddu_dt2, self.ddv_dt2)


@dataclass(frozen=True)
class EpisodeReference:
    """Explicit surface/trajectory reference consumed by the mainline.

    The reference is a value object, not a controller-side stage lookup.  All
    pose, twist, acceleration, progress, and load fields are bound to the
    same sampled trajectory instant.
    """

    desired_pose_base: tuple[float, ...] | Sequence[float]
    desired_twist_base: tuple[float, ...] | Sequence[float]
    desired_acceleration_base: tuple[float, ...] | Sequence[float]
    progress_s: float
    duration_s: float
    target_load_n: float
    preload_n: float
    reaction_normal_base: tuple[float, ...] | Sequence[float]
    frame_id: str = "base"

    def __post_init__(self) -> None:
        for name in ("desired_pose_base", "desired_twist_base", "desired_acceleration_base"):
            values = tuple(float(value) for value in getattr(self, name))
            if len(values) != 6 or not all(math.isfinite(value) for value in values):
                raise ValueError(f"{name} must contain six finite values")
            object.__setattr__(self, name, values)
        reaction = tuple(float(value) for value in self.reaction_normal_base)
        if len(reaction) != 3 or not all(math.isfinite(value) for value in reaction):
            raise ValueError("reaction_normal_base must contain three finite values")
        reaction_norm = math.sqrt(sum(value * value for value in reaction))
        if abs(reaction_norm - 1.0) > 1e-6:
            raise ValueError("reaction_normal_base must be unit length")
        object.__setattr__(self, "reaction_normal_base", reaction)
        scalars = (self.progress_s, self.duration_s, self.target_load_n, self.preload_n)
        if not all(math.isfinite(float(value)) for value in scalars):
            raise ValueError("episode reference scalars must be finite")
        if self.progress_s < 0.0 or self.duration_s <= 0.0:
            raise ValueError("episode reference progress/duration is invalid")
        if self.target_load_n < 0.0 or self.preload_n < 0.0:
            raise ValueError("episode reference loads must be non-negative")
        if not str(self.frame_id).strip():
            raise ValueError("episode reference frame_id must be non-empty")

    @property
    def desired_xy(self) -> tuple[float, float]:
        return (self.desired_pose_base[0], self.desired_pose_base[1])

    @property
    def desired_velocity_xy(self) -> tuple[float, float]:
        return (self.desired_twist_base[0], self.desired_twist_base[1])

    def as_mapping(self) -> dict[str, object]:
        return {
            "desired_pose_base": self.desired_pose_base,
            "desired_twist_base": self.desired_twist_base,
            "desired_acceleration_base": self.desired_acceleration_base,
            "progress_s": self.progress_s,
            "duration_s": self.duration_s,
            "target_load_n": self.target_load_n,
            "preload_n": self.preload_n,
            "reaction_normal_base": self.reaction_normal_base,
            "frame_id": self.frame_id,
            "desired_xy": self.desired_xy,
            "desired_velocity_xy": self.desired_velocity_xy,
        }


@dataclass(frozen=True)
class BoundedTrajectory:
    schema_version: str
    family: str
    seed: int
    duration_s: float
    rate_hz: int
    profile: TrajectoryProfile
    samples: tuple[TrajectorySample, ...]

    @property
    def max_speed_m_s(self) -> float:
        return max(sample.speed_m_s for sample in self.samples)

    @property
    def max_acceleration_m_s2(self) -> float:
        return max(sample.acceleration_m_s2 for sample in self.samples)

    @property
    def max_curvature_m_inv(self) -> float:
        return max(sample.curvature_m_inv for sample in self.samples)

    def base_positions(self, surface: SurfaceCalibration, *, normal_offset_m: float = 0.0) -> tuple[tuple[float, float, float], ...]:
        return tuple(
            surface.normalized_to_base(sample.u_normalized, sample.v_normalized, normal_offset_m)
            for sample in self.samples
        )

    def episode_reference(self, surface: SurfaceCalibration, index: int, *, normal_offset_m: float = 0.0) -> EpisodeReference:
        """Return an explicit reference row for mainline injection."""

        if index < 0 or index >= len(self.samples):
            raise IndexError("trajectory reference index is outside sampled trajectory")
        sample = self.samples[index]
        position = surface.normalized_to_base(sample.u_normalized, sample.v_normalized, normal_offset_m)
        safe_u_half = surface.width_m / 2.0 - surface.safe_inset_m
        safe_v_half = surface.height_m / 2.0 - surface.safe_inset_m
        velocity = (
            surface.u_axis_base[0] * safe_u_half * sample.du_dt
            + surface.v_axis_base[0] * safe_v_half * sample.dv_dt,
            surface.u_axis_base[1] * safe_u_half * sample.du_dt
            + surface.v_axis_base[1] * safe_v_half * sample.dv_dt,
            surface.u_axis_base[2] * safe_u_half * sample.du_dt
            + surface.v_axis_base[2] * safe_v_half * sample.dv_dt,
        )
        acceleration = (
            surface.u_axis_base[0] * safe_u_half * sample.ddu_dt2
            + surface.v_axis_base[0] * safe_v_half * sample.ddv_dt2,
            surface.u_axis_base[1] * safe_u_half * sample.ddu_dt2
            + surface.v_axis_base[1] * safe_v_half * sample.ddv_dt2,
            surface.u_axis_base[2] * safe_u_half * sample.ddu_dt2
            + surface.v_axis_base[2] * safe_v_half * sample.ddv_dt2,
        )
        orientation = _canonical_quaternion_to_rotvec(surface.corners[0].orientation_xyzw)
        return EpisodeReference(
            desired_pose_base=(*position, *orientation),
            desired_twist_base=(*velocity, 0.0, 0.0, 0.0),
            desired_acceleration_base=(*acceleration, 0.0, 0.0, 0.0),
            progress_s=sample.timestamp_s,
            duration_s=self.duration_s,
            target_load_n=sample.normal_force_target_n,
            preload_n=sample.preload_n,
            reaction_normal_base=surface.reaction_normal_base,
            frame_id=surface.frame_id,
        )


def _profile_for_episode(index: int, seed: int) -> TrajectoryProfile:
    rng = random.Random(seed + 7919 * index)
    return TrajectoryProfile(
        speed_scale=0.55 + 0.35 * rng.random(),
        normal_force_target_n=3.0 + 5.0 * rng.random(),
        preload_n=0.3 + 1.2 * rng.random(),
    )


def episode_plan(count: int = 50, *, seed: int = 42) -> tuple[tuple[int, str, TrajectoryProfile], ...]:
    if count <= 0:
        raise ValueError("episode count must be positive")
    return tuple(
        (index, TRAJECTORY_FAMILIES[index % len(TRAJECTORY_FAMILIES)], _profile_for_episode(index, seed))
        for index in range(count)
    )


def _path_function(family: str, seed: int) -> Callable[[float], tuple[float, float, float, float, float, float]]:
    if family not in TRAJECTORY_FAMILIES:
        raise ValueError(f"unsupported trajectory family: {family}")
    rng = random.Random(seed)
    phase = rng.uniform(-math.pi, math.pi)

    def circle(theta: float):
        return (0.78 * math.cos(theta), 0.78 * math.sin(theta), -0.78 * math.sin(theta), 0.78 * math.cos(theta), -0.78 * math.cos(theta), -0.78 * math.sin(theta))

    def ellipse(theta: float):
        return (0.84 * math.cos(theta), 0.52 * math.sin(theta), -0.84 * math.sin(theta), 0.52 * math.cos(theta), -0.84 * math.cos(theta), -0.52 * math.sin(theta))

    def figure_eight(theta: float):
        s, c = math.sin(theta), math.cos(theta)
        return (0.82 * s, 0.62 * s * c, 0.82 * c, 0.62 * (c * c - s * s), -0.82 * s, -1.24 * s * c)

    def lissajous(theta: float):
        a = theta + phase
        return (0.48 * math.sin(2.0 * a), 0.32 * math.sin(3.0 * theta), 0.96 * math.cos(2.0 * a), 0.96 * math.cos(3.0 * theta), -1.92 * math.sin(2.0 * a), -2.88 * math.sin(3.0 * theta))

    def linear_grid(theta: float):
        # A smooth raster surrogate: long nearly-linear sweeps joined by
        # cosine turns, avoiding derivative discontinuities at corners.
        return (0.84 * math.sin(theta), 0.62 * math.sin(2.0 * theta), 0.84 * math.cos(theta), 1.24 * math.cos(2.0 * theta), -0.84 * math.sin(theta), -2.48 * math.sin(2.0 * theta))

    def rounded_arc(theta: float):
        a = theta + phase / 3.0
        return (0.76 * math.cos(a), 0.28 + 0.43 * math.sin(a), -0.76 * math.sin(a), 0.43 * math.cos(a), -0.76 * math.cos(a), -0.43 * math.sin(a))

    def smooth_spline(theta: float):
        # A seeded, C-infinity Fourier spline around a non-zero-speed base
        # loop.  Coefficients are fixed and the seed only changes phase, so
        # the path is intrinsically inside the normalized safe square.
        a = theta + phase
        b = theta - phase / 2.0
        u = 0.58 * math.sin(a) + 0.12 * math.sin(2.0 * a) + 0.06 * math.sin(3.0 * a)
        v = 0.50 * math.cos(b) + 0.10 * math.cos(2.0 * b) + 0.05 * math.cos(3.0 * b)
        du = 0.58 * math.cos(a) + 0.24 * math.cos(2.0 * a) + 0.18 * math.cos(3.0 * a)
        dv = -0.50 * math.sin(b) - 0.20 * math.sin(2.0 * b) - 0.15 * math.sin(3.0 * b)
        ddu = -0.58 * math.sin(a) - 0.48 * math.sin(2.0 * a) - 0.54 * math.sin(3.0 * a)
        ddv = -0.50 * math.cos(b) - 0.40 * math.cos(2.0 * b) - 0.45 * math.cos(3.0 * b)
        return (u, v, du, dv, ddu, ddv)

    return {
        "circle": circle,
        "ellipse": ellipse,
        "figure_eight": figure_eight,
        "lissajous": lissajous,
        "linear_grid": linear_grid,
        "rounded_arc": rounded_arc,
        "seeded_smooth_spline": smooth_spline,
    }[family]


def generate_bounded_trajectory(
    surface: SurfaceCalibration,
    *,
    family: str,
    seed: int,
    duration_s: float = 8.0,
    rate_hz: int = 500,
    profile: TrajectoryProfile | None = None,
    max_speed_m_s: float = 0.04,
    max_acceleration_m_s2: float = 0.8,
    max_curvature_m_inv: float = 5000.0,
) -> BoundedTrajectory:
    if duration_s <= 0.0 or not math.isfinite(duration_s):
        raise ValueError("duration_s must be positive and finite")
    if rate_hz <= 0:
        raise ValueError("rate_hz must be positive")
    if max_speed_m_s <= 0.0 or max_acceleration_m_s2 <= 0.0 or max_curvature_m_inv <= 0.0:
        raise ValueError("trajectory limits must be positive")
    if not math.isfinite(max_curvature_m_inv):
        raise ValueError("max_curvature_m_inv must be finite")
    selected_profile = profile or _profile_for_episode(seed, seed)
    function = _path_function(family, seed)
    u_half = surface.width_m / 2.0 - surface.safe_inset_m
    v_half = surface.height_m / 2.0 - surface.safe_inset_m
    count = int(round(duration_s * rate_hz)) + 1
    samples: list[TrajectorySample] = []
    for index in range(count):
        timestamp = min(duration_s, index / rate_hz)
        theta = 2.0 * math.pi * timestamp / duration_s * selected_profile.speed_scale
        raw_u, raw_v, raw_du, raw_dv, raw_ddu, raw_ddv = function(theta)
        theta_rate = 2.0 * math.pi * selected_profile.speed_scale / duration_s
        # Never clip position independently of its analytic derivatives.  A
        # clipped sample would no longer describe the commanded path.  Each
        # family is intrinsically bounded; a seeded spline that violates the
        # contract fails before a trajectory can be returned.
        if abs(raw_u) > 1.0 + 1e-12 or abs(raw_v) > 1.0 + 1e-12:
            raise ValueError("trajectory normalized position exceeds intrinsic bounds")
        u = raw_u
        v = raw_v
        du = raw_du * theta_rate
        dv = raw_dv * theta_rate
        ddu = raw_ddu * theta_rate * theta_rate
        ddv = raw_ddv * theta_rate * theta_rate
        speed_m_s = math.hypot(u_half * du, v_half * dv)
        acceleration_m_s2 = math.hypot(u_half * ddu, v_half * ddv)
        tangent_u = u_half * du
        tangent_v = v_half * dv
        normal_accel_u = u_half * ddu
        normal_accel_v = v_half * ddv
        speed_for_curvature = math.hypot(tangent_u, tangent_v)
        curvature_m_inv = (
            abs(tangent_u * normal_accel_v - tangent_v * normal_accel_u)
            / speed_for_curvature**3
            if speed_for_curvature > 1e-12
            else 0.0
        )
        if speed_m_s > max_speed_m_s + 1e-9:
            raise ValueError(f"trajectory speed bound exceeded: {speed_m_s:.6g} m/s")
        if acceleration_m_s2 > max_acceleration_m_s2 + 1e-9:
            raise ValueError(f"trajectory acceleration bound exceeded: {acceleration_m_s2:.6g} m/s^2")
        if curvature_m_inv > max_curvature_m_inv + 1e-9:
            raise ValueError(f"trajectory curvature bound exceeded: {curvature_m_inv:.6g} 1/m")
        samples.append(TrajectorySample(timestamp, u, v, du, dv, ddu, ddv, speed_m_s, acceleration_m_s2, curvature_m_inv, selected_profile.normal_force_target_n, selected_profile.preload_n))
    return BoundedTrajectory("ur10e_tacdiffusion_trajectory/v1", family, int(seed), float(duration_s), int(rate_hz), selected_profile, tuple(samples))
