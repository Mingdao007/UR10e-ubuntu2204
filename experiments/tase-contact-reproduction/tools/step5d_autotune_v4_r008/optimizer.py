"""r008 optimizer: GP feature map on log2(P/D) and periodic refits."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from step5d_autotune_v4_r006.lattice import ParameterPoint
from step5d_autotune_v4_r006.optimizer import (
    ConditionalMatern52GP,
    GPObservation,
    OptimizerError,
    feature_map as r006_feature_map,
)

from .lattice import R008Point, BoxRegion, scrambled_sobol, point_from_physical


REFIT_EVERY = 4
# Restored to 64 after Stage A batched qLogNEI scoring (r008 overlay): one
# t-batched acquisition forward makes candidate count near-constant-cost, so
# the 32 cut (workaround for ~4.7s/candidate serial scoring) is no longer
# needed. Power-of-2 keeps scrambled_sobol balance.
CANDIDATE_SOBOL = 64


def feature_map_r008(point: ParameterPoint) -> tuple[float, ...]:
    """ARD-aligned features: axis 0 is log2(P/D), the performance coordinate."""

    p = point.p_gain
    d = point.d_gain
    return (
        math.log2(p / d),
        math.log2(d),
        0.0 if point.i_gain == 0.0 else math.log2(point.i_gain / p),
        math.log2(point.tau_s),
        math.log2(point.ko),
        math.log2(point.kp),
        0.0 if point.i_gain == 0.0 else 1.0,
    )


@dataclass
class R008OptimizerState:
    observations: list[GPObservation]
    gp: ConditionalMatern52GP
    fits: int = 0

    def observe(self, observation: GPObservation) -> None:
        self.observations.append(observation)
        if len(self.observations) % REFIT_EVERY == 0 or self.fits < 2:
            self.gp.fit(self.observations)  # type: ignore[attr-defined]
            self.fits += 1


def propose_candidates(
    box: BoxRegion,
    *,
    count: int = CANDIDATE_SOBOL,
    seed: int = 8,
) -> tuple[ParameterPoint, ...]:
    points = scrambled_sobol(box, count=count, seed=seed)
    return tuple(point.to_parameter_point() for point in points)


__all__ = [
    "CANDIDATE_SOBOL",
    "REFIT_EVERY",
    "R008OptimizerState",
    "feature_map_r008",
    "propose_candidates",
    "r006_feature_map",
]
