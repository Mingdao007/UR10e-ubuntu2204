"""Pinned discretization of TacDiffusion's dynamic force filter."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence


PAPER_ALPHA = 0.9
PAPER_BETA = 0.3
FILTER_RATE_HZ = 500


def _six(values: Sequence[float], name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) != 6 or not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} must contain six finite values")
    return result


@dataclass(frozen=True)
class DynamicForceFilterState:
    filtered_f_ff: tuple[float, ...]
    filtered_f_ff_velocity: tuple[float, ...]


class DynamicForceFilter:
    """Semi-implicit Euler implementation of the paper's second-order filter.

    The continuous equation is ``F_ff_ddot = alpha * (beta * (F_df -
    F_ff) - F_ff_dot)``.  This implementation pins one deterministic 500 Hz
    discretization: update velocity from acceleration, then position from the
    new velocity.  Both states initialize to zero, as specified in the paper.
    """

    def __init__(
        self,
        *,
        alpha: float = PAPER_ALPHA,
        beta: float = PAPER_BETA,
        rate_hz: int = FILTER_RATE_HZ,
    ) -> None:
        if not math.isclose(alpha, PAPER_ALPHA, rel_tol=0.0, abs_tol=1e-15):
            raise ValueError("TacDiffusion alpha is pinned to 0.9")
        if not math.isclose(beta, PAPER_BETA, rel_tol=0.0, abs_tol=1e-15):
            raise ValueError("TacDiffusion beta is pinned to 0.3")
        if rate_hz != FILTER_RATE_HZ:
            raise ValueError("UR10e TacDiffusion filter rate is pinned to 500 Hz")
        self.alpha = float(alpha)
        self.beta = float(beta)
        self.rate_hz = int(rate_hz)
        self.dt_s = 1.0 / self.rate_hz
        self._position = (0.0,) * 6
        self._velocity = (0.0,) * 6

    @property
    def state(self) -> DynamicForceFilterState:
        return DynamicForceFilterState(self._position, self._velocity)

    def reset(self) -> DynamicForceFilterState:
        self._position = (0.0,) * 6
        self._velocity = (0.0,) * 6
        return self.state

    def step(self, raw_f_df: Sequence[float]) -> DynamicForceFilterState:
        target = _six(raw_f_df, "raw_f_df")
        acceleration = tuple(
            self.alpha
            * (self.beta * (target[index] - self._position[index]) - self._velocity[index])
            for index in range(6)
        )
        velocity = tuple(
            self._velocity[index] + self.dt_s * acceleration[index]
            for index in range(6)
        )
        position = tuple(
            self._position[index] + self.dt_s * velocity[index]
            for index in range(6)
        )
        if not all(math.isfinite(value) for value in position + velocity):
            raise ValueError("dynamic force filter produced non-finite state")
        self._position = position
        self._velocity = velocity
        return self.state
