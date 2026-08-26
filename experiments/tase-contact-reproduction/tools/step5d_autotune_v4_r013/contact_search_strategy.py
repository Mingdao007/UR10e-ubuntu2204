"""Typed R013 contact-search schedules.

The force controller, candidate and runtime strategy remain unchanged.  This
module owns only the downward search velocity command so A0 and A1 can be
compared from the same deterministic contract before a live canary.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


CONTACT_SEARCH_STRATEGY_SCHEMA = "step5d.autotune-v4/r013-contact-search-strategy-v1"
A0_HARD_TWO_STAGE = "A0_HARD_TWO_STAGE_V1"
A1_BOUNDED_TANH_SIGMOID = "A1_BOUNDED_TANH_SIGMOID_V1"

DEFAULT_BOUNDARY_M = 0.011029311
DEFAULT_TRANSITION_WIDTH_M = 0.010
DEFAULT_STEEPNESS = 6.0
DEFAULT_FAR_SPEED_M_S = 0.005
DEFAULT_NEAR_SPEED_M_S = 0.0002


class ContactSearchStrategyError(ValueError):
    """A force-search strategy is malformed or outside its safe bounds."""


def _finite(value: Any, role: str) -> float:
    if isinstance(value, bool):
        raise ContactSearchStrategyError(f"{role} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ContactSearchStrategyError(f"{role} must be a finite number") from exc
    if not math.isfinite(result):
        raise ContactSearchStrategyError(f"{role} must be a finite number")
    return result


@dataclass(frozen=True)
class ContactSearchStrategy:
    """A bounded positive speed schedule used by the negative-Z TP command."""

    strategy_id: str
    boundary_m: float = DEFAULT_BOUNDARY_M
    transition_width_m: float = DEFAULT_TRANSITION_WIDTH_M
    steepness: float = DEFAULT_STEEPNESS
    far_speed_m_s: float = DEFAULT_FAR_SPEED_M_S
    near_speed_m_s: float = DEFAULT_NEAR_SPEED_M_S

    def __post_init__(self) -> None:
        if self.strategy_id not in {A0_HARD_TWO_STAGE, A1_BOUNDED_TANH_SIGMOID}:
            raise ContactSearchStrategyError("unknown contact-search strategy")
        for role, value in (
            ("boundary_m", self.boundary_m),
            ("transition_width_m", self.transition_width_m),
            ("steepness", self.steepness),
            ("far_speed_m_s", self.far_speed_m_s),
            ("near_speed_m_s", self.near_speed_m_s),
        ):
            _finite(value, role)
        if self.boundary_m <= 0.0 or self.transition_width_m <= 0.0:
            raise ContactSearchStrategyError("contact-search distances must be positive")
        if self.steepness <= 0.0:
            raise ContactSearchStrategyError("sigmoid steepness must be positive")
        if self.far_speed_m_s <= 0.0 or self.near_speed_m_s <= 0.0:
            raise ContactSearchStrategyError("search speeds must be positive")
        if self.far_speed_m_s < self.near_speed_m_s:
            raise ContactSearchStrategyError("far speed must be >= near speed")

    def speed_m_s(self, travel_m: float) -> float:
        """Return the commanded positive downward speed at travelled depth."""

        travel = _finite(travel_m, "travel_m")
        if self.strategy_id == A0_HARD_TWO_STAGE:
            return self.far_speed_m_s if travel < self.boundary_m else self.near_speed_m_s
        start = self.boundary_m - self.transition_width_m
        if travel <= start:
            return self.far_speed_m_s
        if travel >= self.boundary_m:
            return self.near_speed_m_s
        u = (travel - start) / self.transition_width_m
        lower = math.tanh(-self.steepness)
        upper = math.tanh(self.steepness)
        bounded = (
            math.tanh(self.steepness * (2.0 * u - 1.0)) - lower
        ) / (upper - lower)
        return self.far_speed_m_s + (self.near_speed_m_s - self.far_speed_m_s) * bounded

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": CONTACT_SEARCH_STRATEGY_SCHEMA,
            "strategy_id": self.strategy_id,
            "boundary_m": self.boundary_m,
            "transition_width_m": self.transition_width_m,
            "steepness": self.steepness,
            "far_speed_m_s": self.far_speed_m_s,
            "near_speed_m_s": self.near_speed_m_s,
            "transition_start_m": (
                self.boundary_m - self.transition_width_m
                if self.strategy_id == A1_BOUNDED_TANH_SIGMOID
                else self.boundary_m
            ),
            "transition_end_m": self.boundary_m,
            "formula": (
                "bounded_tanh(u) = (tanh(k*(2u-1))-tanh(-k)) / "
                "(tanh(k)-tanh(-k)); speed = far + (near-far)*bounded_tanh(u)"
                if self.strategy_id == A1_BOUNDED_TANH_SIGMOID
                else "speed = far when travel < boundary, otherwise near"
            ),
        }


def a0_strategy() -> ContactSearchStrategy:
    return ContactSearchStrategy(strategy_id=A0_HARD_TWO_STAGE)


def a1_strategy() -> ContactSearchStrategy:
    return ContactSearchStrategy(strategy_id=A1_BOUNDED_TANH_SIGMOID)


def schedule_rows(
    *,
    max_travel_m: float = 0.025,
    points: int = 101,
) -> list[dict[str, float | str]]:
    """Return chart-ready rows for A0/A1 without inventing observed data."""

    if points < 2:
        raise ContactSearchStrategyError("schedule chart needs at least two points")
    max_travel = _finite(max_travel_m, "max_travel_m")
    if max_travel <= 0.0:
        raise ContactSearchStrategyError("max_travel_m must be positive")
    strategies = (a0_strategy(), a1_strategy())
    rows: list[dict[str, float | str]] = []
    for index in range(points):
        travel = max_travel * index / (points - 1)
        for strategy in strategies:
            rows.append(
                {
                    "travel_mm": travel * 1000.0,
                    "speed_mm_s": strategy.speed_m_s(travel) * 1000.0,
                    "series": strategy.strategy_id,
                    "boundary_mm": strategy.boundary_m * 1000.0,
                    "transition_width_mm": strategy.transition_width_m * 1000.0,
                }
            )
    return rows


__all__ = [
    "A0_HARD_TWO_STAGE",
    "A1_BOUNDED_TANH_SIGMOID",
    "CONTACT_SEARCH_STRATEGY_SCHEMA",
    "ContactSearchStrategy",
    "ContactSearchStrategyError",
    "a0_strategy",
    "a1_strategy",
    "schedule_rows",
]
