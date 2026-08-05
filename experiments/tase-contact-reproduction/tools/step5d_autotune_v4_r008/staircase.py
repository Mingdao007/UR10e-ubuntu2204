"""Bandwidth staircase strategy for r008 Stage D2."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence
import math

from .lattice import R008Point, default_anchor


class StaircaseError(ValueError):
    """Staircase plan is inconsistent."""


@dataclass(frozen=True)
class StaircaseLevel:
    index: int
    scale: float
    point: R008Point
    kind: str = "STAIRCASE"


def build_staircase(
    anchor: R008Point | None = None,
    *,
    scales: Sequence[float] = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0),
) -> tuple[StaircaseLevel, ...]:
    """Geometric P/D staircase with all other axes fixed at the design anchor.

    Level 0 is the r006-era operating point only when ``anchor`` is overridden
    to that physical candidate; the default uses the r008 design anchor.
    """

    base = anchor or default_anchor()
    levels: list[StaircaseLevel] = []
    for index, scale in enumerate(scales):
        if float(scale) <= 0.0:
            raise StaircaseError("staircase scale must be positive")
        point = R008Point(
            log2_pd=math.log2(base.pd_ratio * float(scale)),
            log2_d=base.log2_d,
            log2_tau=base.log2_tau,
            kf_off=base.kf_off,
            log2_kf=base.log2_kf,
            log2_ko=base.log2_ko,
            log2_kp=base.log2_kp,
        )
        levels.append(StaircaseLevel(index=index, scale=float(scale), point=point))
    return tuple(levels)


def edge_half_anchor(levels: Sequence[StaircaseLevel], last_passed_index: int) -> R008Point:
    if last_passed_index < 0 or last_passed_index >= len(levels):
        raise StaircaseError("last_passed_index out of range")
    passed = levels[last_passed_index]
    # ω_c^edge/2 ⇒ P/D halved from the last passing level.
    return R008Point(
        log2_pd=math.log2(passed.point.pd_ratio * 0.5),
        log2_d=passed.point.log2_d,
        log2_tau=passed.point.log2_tau,
        kf_off=passed.point.kf_off,
        log2_kf=passed.point.log2_kf,
        log2_ko=passed.point.log2_ko,
        log2_kp=passed.point.log2_kp,
    )


__all__ = ["StaircaseError", "StaircaseLevel", "build_staircase", "edge_half_anchor"]
