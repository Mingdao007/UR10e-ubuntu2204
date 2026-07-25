"""Rate-invariant second-order force filter using explicit settling time."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Sequence


def _six(values: Iterable[float], name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) != 6 or not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} must contain six finite values")
    return result


@dataclass(frozen=True)
class DynamicFilterProfile:
    settling_time_s: float = 0.05
    damping_ratio: float = 1.0
    rate_hz: int = 500

    def __post_init__(self) -> None:
        if not math.isfinite(self.settling_time_s) or self.settling_time_s <= 0.0:
            raise ValueError("settling_time_s must be positive and finite")
        if not math.isfinite(self.damping_ratio) or self.damping_ratio <= 0.0:
            raise ValueError("damping_ratio must be positive and finite")
        if self.rate_hz <= 0:
            raise ValueError("rate_hz must be positive")

    @property
    def natural_frequency_rad_s(self) -> float:
        # For a critical second-order step, exp(-x) * (1+x) = 0.02 at
        # x ~= 5.83392.  Using 4/Ts would leave roughly 9.2% error at Ts.
        return 5.83392170191739 / (self.damping_ratio * self.settling_time_s)


@dataclass(frozen=True)
class DynamicFilterState:
    filtered_f_ff: tuple[float, ...]
    filter_velocity: tuple[float, ...]


def _advance(position: float, velocity: float, target: float, dt_s: float, omega: float, zeta: float) -> tuple[float, float]:
    y = position - target
    if zeta < 1.0 - 1e-9:
        wd = omega * math.sqrt(1.0 - zeta * zeta)
        exponential = math.exp(-zeta * omega * dt_s)
        cosine = math.cos(wd * dt_s)
        sine = math.sin(wd * dt_s)
        c = (velocity + zeta * omega * y) / wd
        new_y = exponential * (y * cosine + c * sine)
        new_velocity = exponential * (velocity * cosine - (wd * y + zeta * omega * c) * sine)
    elif abs(zeta - 1.0) <= 1e-9:
        exponential = math.exp(-omega * dt_s)
        c = velocity + omega * y
        new_y = exponential * (y + c * dt_s)
        new_velocity = exponential * (velocity - omega * c * dt_s)
    else:
        root = math.sqrt(zeta * zeta - 1.0)
        r1 = -omega * (zeta - root)
        r2 = -omega * (zeta + root)
        a = (velocity - r2 * y) / (r1 - r2)
        b = y - a
        e1, e2 = math.exp(r1 * dt_s), math.exp(r2 * dt_s)
        new_y = a * e1 + b * e2
        new_velocity = r1 * a * e1 + r2 * b * e2
    result = (target + new_y, new_velocity)
    if not all(math.isfinite(value) for value in result):
        raise ValueError("dynamic filter produced non-finite state")
    return result


class RateInvariantForceFilter:
    def __init__(self, profile: DynamicFilterProfile = DynamicFilterProfile()) -> None:
        self.profile = profile
        self._position = (0.0,) * 6
        self._velocity = (0.0,) * 6

    @property
    def state(self) -> DynamicFilterState:
        return DynamicFilterState(self._position, self._velocity)

    def reset(self) -> DynamicFilterState:
        self._position = (0.0,) * 6
        self._velocity = (0.0,) * 6
        return self.state

    def step(self, raw_f_df: Sequence[float], *, dt_s: float | None = None) -> DynamicFilterState:
        target = _six(raw_f_df, "raw_f_df")
        dt = 1.0 / self.profile.rate_hz if dt_s is None else float(dt_s)
        if not math.isfinite(dt) or dt <= 0.0:
            raise ValueError("filter dt_s must be positive and finite")
        values = tuple(_advance(position, velocity, target[index], dt, self.profile.natural_frequency_rad_s, self.profile.damping_ratio) for index, (position, velocity) in enumerate(zip(self._position, self._velocity)))
        self._position = tuple(value[0] for value in values)
        self._velocity = tuple(value[1] for value in values)
        return self.state

    def smooth_to_zero(self, *, dt_s: float | None = None) -> DynamicFilterState:
        return self.step((0.0,) * 6, dt_s=dt_s)
