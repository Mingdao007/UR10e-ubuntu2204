"""Bounded seven-family reference timelines for TacDiffusion formal V4."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping, Sequence

from .trajectory import TRAJECTORY_FAMILIES, _path_function


FORMAL_TRAJECTORY_RATE_HZ = 500
FORMAL_EPISODE_DURATION_S = 8.0
FORMAL_PATH_HALF_WIDTH_M = 0.003


def _smooth_progress(phase: float) -> tuple[float, float, float]:
    """Quintic 0..1 progress and derivatives with zero endpoint velocity/accel."""

    p = min(1.0, max(0.0, float(phase)))
    s = 10.0 * p**3 - 15.0 * p**4 + 6.0 * p**5
    ds = 30.0 * p**2 - 60.0 * p**3 + 30.0 * p**4
    dds = 60.0 * p - 180.0 * p**2 + 120.0 * p**3
    return s, ds, dds


@dataclass(frozen=True)
class FormalTrajectoryRowV1:
    family: str
    seed: int
    progress_s: float
    desired_pose_base: tuple[float, ...]
    desired_twist_base: tuple[float, ...]
    desired_acceleration_base: tuple[float, ...]
    reference_sample_id: str

    def as_mapping(self) -> dict[str, object]:
        return {
            "family": self.family,
            "seed": self.seed,
            "progress_s": self.progress_s,
            "desired_pose_base": self.desired_pose_base,
            "desired_twist_base": self.desired_twist_base,
            "desired_acceleration_base": self.desired_acceleration_base,
            "reference_sample_id": self.reference_sample_id,
            "reference_derivatives_valid": True,
            "frame_id": "base",
        }


@dataclass(frozen=True)
class FormalTrajectoryTimelineV1:
    family: str
    seed: int
    duration_s: float
    rate_hz: int
    rows: tuple[FormalTrajectoryRowV1, ...]
    max_translation_speed_m_s: float
    max_translation_acceleration_m_s2: float
    max_anchor_excursion_m: float

    def row_at(self, elapsed_s: float) -> Mapping[str, object]:
        bounded = min(self.duration_s, max(0.0, float(elapsed_s)))
        index = min(len(self.rows) - 1, int(math.floor(bounded * self.rate_hz + 1e-12)))
        return self.rows[index].as_mapping()


def build_formal_trajectory_timeline(
    *,
    family: str,
    seed: int,
    anchor_pose_base: Sequence[float],
    u_axis_base: Sequence[float],
    v_axis_base: Sequence[float],
    duration_s: float = FORMAL_EPISODE_DURATION_S,
    rate_hz: int = FORMAL_TRAJECTORY_RATE_HZ,
    half_width_m: float = FORMAL_PATH_HALF_WIDTH_M,
) -> FormalTrajectoryTimelineV1:
    if family not in TRAJECTORY_FAMILIES:
        raise ValueError("formal trajectory family is invalid")
    anchor = tuple(float(value) for value in anchor_pose_base)
    u_axis = tuple(float(value) for value in u_axis_base)
    v_axis = tuple(float(value) for value in v_axis_base)
    if len(anchor) != 6 or len(u_axis) != 3 or len(v_axis) != 3:
        raise ValueError("formal trajectory frame shape is invalid")
    if not all(math.isfinite(value) for value in (*anchor, *u_axis, *v_axis)):
        raise ValueError("formal trajectory frame is non-finite")
    if duration_s <= 0.0 or rate_hz != FORMAL_TRAJECTORY_RATE_HZ:
        raise ValueError("formal trajectory duration/rate is invalid")
    if not 0.001 <= half_width_m <= 0.005:
        raise ValueError("formal path half width must be within 1..5 mm")
    for axis, name in ((u_axis, "u"), (v_axis, "v")):
        if abs(math.sqrt(sum(value * value for value in axis)) - 1.0) > 1e-6:
            raise ValueError(f"formal {name} axis must be unit length")
    if abs(sum(left * right for left, right in zip(u_axis, v_axis))) > 1e-6:
        raise ValueError("formal trajectory axes must be orthogonal")
    path = _path_function(family, int(seed))
    origin = path(0.0)
    count = int(round(duration_s * rate_hz)) + 1
    rows: list[FormalTrajectoryRowV1] = []
    max_speed = 0.0
    max_acceleration = 0.0
    max_excursion = 0.0
    for index in range(count):
        t = min(duration_s, index / rate_hz)
        phase = t / duration_s
        smooth, smooth_dp, smooth_ddp = _smooth_progress(phase)
        theta = 2.0 * math.pi * smooth
        theta_d = 2.0 * math.pi * smooth_dp / duration_s
        theta_dd = 2.0 * math.pi * smooth_ddp / (duration_s * duration_s)
        raw_u, raw_v, raw_du, raw_dv, raw_ddu, raw_ddv = path(theta)
        local_u = half_width_m * (raw_u - origin[0])
        local_v = half_width_m * (raw_v - origin[1])
        local_du = half_width_m * raw_du * theta_d
        local_dv = half_width_m * raw_dv * theta_d
        local_ddu = half_width_m * (raw_ddu * theta_d**2 + raw_du * theta_dd)
        local_ddv = half_width_m * (raw_ddv * theta_d**2 + raw_dv * theta_dd)
        position = tuple(
            anchor[axis] + local_u * u_axis[axis] + local_v * v_axis[axis]
            for axis in range(3)
        )
        velocity = tuple(
            local_du * u_axis[axis] + local_dv * v_axis[axis]
            for axis in range(3)
        )
        acceleration = tuple(
            local_ddu * u_axis[axis] + local_ddv * v_axis[axis]
            for axis in range(3)
        )
        max_speed = max(max_speed, math.sqrt(sum(value * value for value in velocity)))
        max_acceleration = max(
            max_acceleration, math.sqrt(sum(value * value for value in acceleration))
        )
        max_excursion = max(max_excursion, math.hypot(local_u, local_v))
        rows.append(
            FormalTrajectoryRowV1(
                family=family,
                seed=int(seed),
                progress_s=t,
                desired_pose_base=position + anchor[3:],
                desired_twist_base=velocity + (0.0, 0.0, 0.0),
                desired_acceleration_base=acceleration + (0.0, 0.0, 0.0),
                reference_sample_id=f"{family}:{seed}:{index}",
            )
        )
    if max_speed > 0.01 + 1e-12:
        raise ValueError("formal trajectory exceeds 10 mm/s receiver limit")
    if max_acceleration > 0.2 + 1e-12:
        raise ValueError("formal trajectory exceeds 0.2 m/s2 qualification limit")
    if math.dist(rows[0].desired_pose_base[:3], anchor[:3]) > 1e-12:
        raise ValueError("formal trajectory does not start at anchor")
    if math.dist(rows[-1].desired_pose_base[:3], anchor[:3]) > 1e-9:
        raise ValueError("formal trajectory does not return to anchor")
    return FormalTrajectoryTimelineV1(
        family=family,
        seed=int(seed),
        duration_s=float(duration_s),
        rate_hz=int(rate_hz),
        rows=tuple(rows),
        max_translation_speed_m_s=max_speed,
        max_translation_acceleration_m_s2=max_acceleration,
        max_anchor_excursion_m=max_excursion,
    )


__all__ = [
    "FORMAL_EPISODE_DURATION_S",
    "FORMAL_PATH_HALF_WIDTH_M",
    "FORMAL_TRAJECTORY_RATE_HZ",
    "FormalTrajectoryRowV1",
    "FormalTrajectoryTimelineV1",
    "build_formal_trajectory_timeline",
]
