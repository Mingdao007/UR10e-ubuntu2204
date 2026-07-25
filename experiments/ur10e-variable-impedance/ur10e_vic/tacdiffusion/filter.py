"""Compatibility facade for the rate-invariant mainline force filter.

The old paper ``alpha/beta`` discretization is not a production option.  The
class name remains for callers of the historical package API, but every
normal instance delegates to :class:`RateInvariantForceFilter`, whose profile
is explicit in seconds, damping ratio, and sample rate.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence

from .dynamic_filter import (
    DynamicFilterProfile,
    RateInvariantForceFilter,
)


FILTER_RATE_HZ = 500


@dataclass(frozen=True)
class DynamicForceFilterState:
    filtered_f_ff: tuple[float, ...]
    filtered_f_ff_velocity: tuple[float, ...]


class DynamicForceFilter:
    """Backward-compatible state naming over the calibrated filter."""

    def __init__(
        self,
        *,
        rate_hz: int = FILTER_RATE_HZ,
        settling_time_s: float = 0.05,
        damping_ratio: float = 1.0,
        alpha: float | None = None,
        beta: float | None = None,
    ) -> None:
        # Accepting the names lets old callers fail closed instead of silently
        # reviving the obsolete equation.
        if alpha is not None:
            raise ValueError("alpha is pinned out of the mainline filter")
        if beta is not None:
            raise ValueError("beta is pinned out of the mainline filter")
        self._delegate = RateInvariantForceFilter(
            DynamicFilterProfile(
                settling_time_s=settling_time_s,
                damping_ratio=damping_ratio,
                rate_hz=rate_hz,
            )
        )

    @property
    def rate_hz(self) -> int:
        return self._delegate.profile.rate_hz

    @property
    def state(self) -> DynamicForceFilterState:
        state = self._delegate.state
        return DynamicForceFilterState(state.filtered_f_ff, state.filter_velocity)
    def reset(self) -> DynamicForceFilterState:
        self._delegate.reset()
        return self.state

    def step(self, raw_f_df: Sequence[float]) -> DynamicForceFilterState:
        state = self._delegate.step(raw_f_df)
        return DynamicForceFilterState(state.filtered_f_ff, state.filter_velocity)
