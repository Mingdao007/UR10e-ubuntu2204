"""r008 controller seams that the frozen r006 closure cannot express.

B1: single-step filter reuse (τ_eff = ρ·τ with ρ≈0.5 matching the double call)
B2: hold/retract/stop clears outer-loop state
B3: integral limit becomes an r008 coordinate
B6: ActiveMotionEnvelopeV3 with 5 mm/s normal cap + slope/divergence/oscillation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping
import math

import numpy as np

from step5d_autotune_v4_r004.path_controller import filter_alpha as production_filter_alpha


class ControllerSeamError(ValueError):
    """Controller seam refused an unsafe or untyped update."""


NORMAL_CAP_M_S = 0.005
SLOPE_LIMIT_M_S = 0.0005  # per tick
DIVERGENCE_WINDOW_S = 0.5
OSCILLATION_WINDOW_S = 1.0
TARGET_FORCE_N = 5.0


@dataclass
class FilterCache:
    """B1: reuse one filter update when the PATH path_controller is stepped twice."""

    tick_id: int | None = None
    filtered_normal_n: float | None = None
    alpha: float | None = None

    def observe(
        self,
        *,
        tick_id: int,
        raw_normal_n: float,
        actual_dt_s: float,
        tau_s: float,
        rho: float,
        previous_filtered_n: float,
    ) -> float:
        if self.tick_id == int(tick_id) and self.filtered_normal_n is not None:
            return float(self.filtered_normal_n)
        alpha = production_filter_alpha(actual_dt_s, float(tau_s) * float(rho))
        filtered = (1.0 - alpha) * float(previous_filtered_n) + alpha * float(raw_normal_n)
        self.tick_id = int(tick_id)
        self.filtered_normal_n = float(filtered)
        self.alpha = float(alpha)
        return float(filtered)


@dataclass
class ActiveMotionEnvelopeV3:
    """B6 host-side normal envelope evaluated before the qdot gate."""

    normal_cap_m_s: float = NORMAL_CAP_M_S
    slope_limit_m_s: float = SLOPE_LIMIT_M_S
    divergence_window_s: float = DIVERGENCE_WINDOW_S
    oscillation_window_s: float = OSCILLATION_WINDOW_S
    design_omega_c_rad_s: float = 3.1
    _prev_u: float = 0.0
    _div_timer_s: float = 0.0
    _prev_error: float = 0.0
    _zero_crossings: int = 0
    _osc_timer_s: float = 0.0

    def reset(self) -> None:
        self._prev_u = 0.0
        self._div_timer_s = 0.0
        self._prev_error = 0.0
        self._zero_crossings = 0
        self._osc_timer_s = 0.0

    def evaluate(
        self,
        *,
        commanded_normal_m_s: float,
        filtered_normal_n: float,
        dt_s: float,
    ) -> tuple[float, str | None]:
        u = float(commanded_normal_m_s)
        dt = float(dt_s)
        if not math.isfinite(u) or not math.isfinite(dt) or dt <= 0.0:
            raise ControllerSeamError("envelope inputs must be finite and dt>0")

        # Slope limit.
        delta = u - self._prev_u
        if abs(delta) > self.slope_limit_m_s:
            u = self._prev_u + math.copysign(self.slope_limit_m_s, delta)

        # Hard normal cap.
        if abs(u) > self.normal_cap_m_s:
            u = math.copysign(self.normal_cap_m_s, u)

        error = float(TARGET_FORCE_N) - float(filtered_normal_n)
        # Divergence: |error| rising while |u| is saturated.
        if abs(u) >= self.normal_cap_m_s * 0.99 and abs(error) > abs(self._prev_error):
            self._div_timer_s += dt
        else:
            self._div_timer_s = 0.0
        if self._div_timer_s > self.divergence_window_s:
            self._prev_u = u
            self._prev_error = error
            return u, "divergence_stop"

        # Oscillation: zero-crossing rate of (f-5) above 2*fc.
        if self._prev_error == 0.0 or error == 0.0 or (self._prev_error < 0.0) != (error < 0.0):
            if self._prev_error != 0.0 and error != 0.0 and (self._prev_error < 0.0) != (error < 0.0):
                self._zero_crossings += 1
        self._osc_timer_s += dt
        if self._osc_timer_s >= self.oscillation_window_s:
            rate = self._zero_crossings / self._osc_timer_s
            limit = 2.0 * (self.design_omega_c_rad_s / (2.0 * math.pi))
            self._zero_crossings = 0
            self._osc_timer_s = 0.0
            if rate > limit:
                self._prev_u = u
                self._prev_error = error
                return u, "oscillation_stop"

        self._prev_u = u
        self._prev_error = error
        return u, None


@dataclass
class OuterLoopStateReset:
    """B2: clear integrator and previous velocity across hold/retract/stop."""

    force_integral_n_s: float = 0.0
    xdot_p_prev_m_s: tuple[float, float, float] = (0.0, 0.0, 0.0)

    def clear(self) -> None:
        self.force_integral_n_s = 0.0
        self.xdot_p_prev_m_s = (0.0, 0.0, 0.0)


@dataclass(frozen=True)
class IntegralLimitCoordinate:
    """B3: searchable integral limit instead of the hard-coded 1.0 N·s."""

    force_integral_limit_n_s: float = 5.0

    def __post_init__(self) -> None:
        value = float(self.force_integral_limit_n_s)
        if not math.isfinite(value) or value <= 0.0:
            raise ControllerSeamError("integral limit must be positive and finite")


@dataclass
class R008ControllerSeam:
    rho: float = 0.5
    filter_cache: FilterCache = field(default_factory=FilterCache)
    envelope: ActiveMotionEnvelopeV3 = field(default_factory=ActiveMotionEnvelopeV3)
    state_reset: OuterLoopStateReset = field(default_factory=OuterLoopStateReset)
    integral_limit: IntegralLimitCoordinate = field(default_factory=IntegralLimitCoordinate)
    _tick_id: int = 0

    def next_tick(self) -> int:
        self._tick_id += 1
        return self._tick_id

    def on_hold_retract_or_stop(self) -> None:
        self.state_reset.clear()
        self.envelope.reset()
        self.filter_cache = FilterCache()


def reproduce_r006_force_with_rho(
    measured_n: np.ndarray,
    dt_s: np.ndarray,
    *,
    tau_s: float,
    rho: float,
) -> np.ndarray:
    """Differential helper: filter a raw-ish force at ρ·τ for seam checks."""

    out = np.empty_like(measured_n, dtype=float)
    filt = float(measured_n[0])
    for index, (dt, value) in enumerate(zip(dt_s, measured_n, strict=True)):
        alpha = production_filter_alpha(float(dt), float(tau_s) * float(rho))
        filt = (1.0 - alpha) * filt + alpha * float(value)
        out[index] = filt
    return out


__all__ = [
    "NORMAL_CAP_M_S",
    "ActiveMotionEnvelopeV3",
    "ControllerSeamError",
    "FilterCache",
    "IntegralLimitCoordinate",
    "OuterLoopStateReset",
    "R008ControllerSeam",
    "reproduce_r006_force_with_rho",
]
