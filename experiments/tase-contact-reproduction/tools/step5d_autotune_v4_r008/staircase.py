"""Bandwidth staircase strategy for r008 Stage D2."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence
import math

from step5d_autotune_v4_r006.contracts import I_ON_ANCHOR, STEP_OCTAVE

from .lattice import (
    CONFIRMED_DANGEROUS_KI,
    R008Point,
    confirmed_dangerous_ki,
    default_anchor,
    snap_to_quarter,
)


class StaircaseError(ValueError):
    """Staircase plan is inconsistent."""


#: Host STAIRCASE refill uses P-up + Ki-lock (not coupled fixed-kf).
#: Coupled ×2 grew Ki with P into/near the confirmed PATH pocket; reject that.
STAIRCASE_USE_P_UP_KI_LOCK = True

#: Default Ki policy for P-up levels. ``i_off`` is preferred: I_OFF never enters
#: the CONFIRMED_DANGEROUS_KI ban, and scale=1/2 clear saturated PM offline
#: (held-safe Ki still fails sat-PM on the design plant).
StaircaseKiLockPolicy = Literal["i_off", "held_safe"]
STAIRCASE_KI_LOCK_POLICY: StaircaseKiLockPolicy = "i_off"

#: held_safe: lattice Ki at pocket_center − 4 quarter-octaves (outside ±1 ban).
HELD_SAFE_KI_STEPS_BELOW_POCKET = 4


@dataclass(frozen=True)
class StaircaseLevel:
    index: int
    scale: float
    point: R008Point
    kind: str = "STAIRCASE"


def _scales_or_default(scales: Sequence[float] | None) -> tuple[float, ...]:
    if scales is None:
        return (1.0, 2.0, 4.0, 8.0, 16.0, 32.0)
    out = tuple(float(s) for s in scales)
    if not out:
        raise StaircaseError("staircase scales must be non-empty")
    return out


def build_staircase_coupled_fixed_kf(
    anchor: R008Point | None = None,
    *,
    scales: Sequence[float] | None = None,
) -> tuple[StaircaseLevel, ...]:
    """Legacy geometric P/D staircase with **kf fixed** (Ki scales with P).

    Kept for regression documentation only. Coupled scale=1 snaps onto
    ``CONFIRMED_DANGEROUS_KI``; scale=2 doubles that Ki. Do **not** use for
    post-pocket physical resume — see ``build_staircase_p_up_ki_lock``.
    """

    base = anchor or default_anchor()
    levels: list[StaircaseLevel] = []
    for index, scale in enumerate(_scales_or_default(scales)):
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


def held_safe_ki_gain() -> float:
    """Lattice Ki outside the confirmed-dangerous ±1 quarter-octave pocket."""

    center_step = int(
        round(math.log2(CONFIRMED_DANGEROUS_KI / I_ON_ANCHOR) / STEP_OCTAVE)
    )
    step = center_step - int(HELD_SAFE_KI_STEPS_BELOW_POCKET)
    ki = float(I_ON_ANCHOR * (2.0 ** (step * STEP_OCTAVE)))
    if confirmed_dangerous_ki(force_i_gain=ki):
        raise StaircaseError(f"held_safe Ki {ki} still inside dangerous pocket")
    return snap_to_quarter(ki, I_ON_ANCHOR)


def _point_p_up_with_ki_lock(
    base: R008Point,
    *,
    scale: float,
    policy: StaircaseKiLockPolicy,
) -> R008Point:
    log2_pd = math.log2(base.pd_ratio * float(scale))
    if policy == "i_off":
        return R008Point(
            log2_pd=log2_pd,
            log2_d=base.log2_d,
            log2_tau=base.log2_tau,
            kf_off=True,
            log2_kf=None,
            log2_ko=base.log2_ko,
            log2_kp=base.log2_kp,
        )
    if policy == "held_safe":
        # Raise P/bandwidth; hold Ki at a fixed safe lattice gain (kf falls as P rises).
        p_gain = float((base.pd_ratio * float(scale)) * base.damping)
        if p_gain <= 0.0:
            raise StaircaseError("P-up scale produced non-positive P")
        ki = held_safe_ki_gain()
        kf = float(ki) / float(p_gain)
        return R008Point(
            log2_pd=log2_pd,
            log2_d=base.log2_d,
            log2_tau=base.log2_tau,
            kf_off=False,
            log2_kf=math.log2(kf),
            log2_ko=base.log2_ko,
            log2_kp=base.log2_kp,
        )
    raise StaircaseError(f"unknown Ki lock policy: {policy!r}")


def build_staircase_p_up_ki_lock(
    anchor: R008Point | None = None,
    *,
    scales: Sequence[float] | None = None,
    ki_policy: StaircaseKiLockPolicy | None = None,
) -> tuple[StaircaseLevel, ...]:
    """Scale ``pd_ratio`` / P while forcing Ki out of the dangerous pocket.

    Default ``ki_policy='i_off'`` (Ki=0). Alternative ``held_safe`` pins Ki to
    ``held_safe_ki_gain()`` and lowers kf as P rises. D/τ/Ko/Kp stay at anchor.
    """

    base = anchor or default_anchor()
    policy: StaircaseKiLockPolicy = ki_policy or STAIRCASE_KI_LOCK_POLICY
    levels: list[StaircaseLevel] = []
    for index, scale in enumerate(_scales_or_default(scales)):
        if float(scale) <= 0.0:
            raise StaircaseError("staircase scale must be positive")
        point = _point_p_up_with_ki_lock(base, scale=float(scale), policy=policy)
        typed = point.to_parameter_point()
        if confirmed_dangerous_ki(force_i_gain=float(typed.i_gain)):
            raise StaircaseError(
                f"P-up Ki-lock level scale={scale} still has dangerous Ki={typed.i_gain}"
            )
        levels.append(StaircaseLevel(index=index, scale=float(scale), point=point))
    return tuple(levels)


def build_staircase(
    anchor: R008Point | None = None,
    *,
    scales: Sequence[float] = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0),
    use_p_up_ki_lock: bool | None = None,
    ki_policy: StaircaseKiLockPolicy | None = None,
) -> tuple[StaircaseLevel, ...]:
    """Host-facing staircase builder.

    Default (``STAIRCASE_USE_P_UP_KI_LOCK``): P-up with Ki locked safe.
    Pass ``use_p_up_ki_lock=False`` for the legacy coupled fixed-kf geometry.
    """

    lock = STAIRCASE_USE_P_UP_KI_LOCK if use_p_up_ki_lock is None else bool(use_p_up_ki_lock)
    if lock:
        return build_staircase_p_up_ki_lock(
            anchor, scales=scales, ki_policy=ki_policy
        )
    return build_staircase_coupled_fixed_kf(anchor, scales=scales)


def edge_half_anchor(levels: Sequence[StaircaseLevel], last_passed_index: int) -> R008Point:
    if last_passed_index < 0 or last_passed_index >= len(levels):
        raise StaircaseError("last_passed_index out of range")
    passed = levels[last_passed_index]
    # ω_c^edge/2 ⇒ P/D halved from the last passing level; preserve Ki lock axes.
    return R008Point(
        log2_pd=math.log2(passed.point.pd_ratio * 0.5),
        log2_d=passed.point.log2_d,
        log2_tau=passed.point.log2_tau,
        kf_off=passed.point.kf_off,
        log2_kf=passed.point.log2_kf,
        log2_ko=passed.point.log2_ko,
        log2_kp=passed.point.log2_kp,
    )


__all__ = [
    "HELD_SAFE_KI_STEPS_BELOW_POCKET",
    "STAIRCASE_KI_LOCK_POLICY",
    "STAIRCASE_USE_P_UP_KI_LOCK",
    "StaircaseError",
    "StaircaseKiLockPolicy",
    "StaircaseLevel",
    "build_staircase",
    "build_staircase_coupled_fixed_kf",
    "build_staircase_p_up_ki_lock",
    "edge_half_anchor",
    "held_safe_ki_gain",
]
