"""Frozen 130-arm QAFS-BO catalog construction."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import math
from typing import Any, Iterable

import numpy as np
from scipy.stats import qmc

from .common import R014Error, sha256_value


FROZEN_CATALOG_SEED = 20260821
SOBOL_ARMS = 128
CATALOG_SIZE = 130
I_MAX = 0.008610779292198037


@dataclass(frozen=True)
class CatalogArm:
    arm_id: str
    force_p_gain: float
    force_damping: float
    force_i_gain: float
    normal_filter_tau_s: float
    orientation_ko: float
    motion_kp: float
    source: str

    @property
    def p_over_d(self) -> float:
        return self.force_p_gain / self.force_damping

    @property
    def i_over_p(self) -> float:
        return self.force_i_gain / self.force_p_gain

    def as_dict(self) -> dict[str, Any]:
        return {
            "arm_id": self.arm_id,
            "force_p_gain": self.force_p_gain,
            "force_damping": self.force_damping,
            "force_i_gain": self.force_i_gain,
            "normal_filter_tau_s": self.normal_filter_tau_s,
            "orientation_ko": self.orientation_ko,
            "motion_kp": self.motion_kp,
            "p_over_d": self.p_over_d,
            "i_over_p": self.i_over_p,
            "source": self.source,
        }


def _log_map(value: float, lower: float, upper: float) -> float:
    return math.exp(math.log(lower) + value * (math.log(upper) - math.log(lower)))


def _sobol_arm(index: int, row: Iterable[float]) -> CatalogArm:
    u = list(float(value) for value in row)
    ratio = _log_map(u[0], 1.25e-5, 4.0e-4)
    damping = _log_map(u[1], 7.0, 224.0)
    p_gain = ratio * damping
    tau = _log_map(u[2], 0.04375, 0.0735784)
    ko = _log_map(u[3], 0.05, 0.8)
    kp = _log_map(u[4], 1.5, 6.0)
    feasible_upper = min(0.5, I_MAX / p_gain)
    if feasible_upper < 0.05:
        raise R014Error("Sobol arm has no feasible I/P interval")
    i_over_p = _log_map(u[5], 0.05, feasible_upper)
    i_gain = p_gain * i_over_p
    return CatalogArm(
        arm_id=f"sobol-{index:03d}",
        force_p_gain=p_gain,
        force_damping=damping,
        force_i_gain=i_gain,
        normal_filter_tau_s=tau,
        orientation_ko=ko,
        motion_kp=kp,
        source="owen-scrambled-sobol",
    )


@lru_cache(maxsize=1)
def _cached_frozen_catalog() -> tuple[CatalogArm, ...]:
    sampler = qmc.Sobol(d=6, scramble=True, seed=FROZEN_CATALOG_SEED)
    rows = sampler.random_base2(m=7)
    arms = [_sobol_arm(index, row) for index, row in enumerate(rows)]
    arms.extend(
        (
            CatalogArm(
                arm_id="human-anchor",
                force_p_gain=0.0003535533906,
                force_damping=28.0,
                force_i_gain=0.00001,
                normal_filter_tau_s=0.35,
                orientation_ko=0.1,
                motion_kp=1.5,
                source="human-anchor",
            ),
            CatalogArm(
                arm_id="r013-incumbent-coordinates",
                force_p_gain=0.019027313840405524,
                force_damping=188.36079701683204,
                force_i_gain=I_MAX,
                normal_filter_tau_s=0.04375,
                orientation_ko=0.05,
                motion_kp=2.5226892457611436,
                source="coordinates-only-no-historical-objective",
            ),
        )
    )
    validate_catalog(arms)
    return tuple(arms)


def build_frozen_catalog() -> tuple[CatalogArm, ...]:
    """Return the immutable deterministic catalog shared by all callers."""

    return _cached_frozen_catalog()


def validate_catalog(arms: Iterable[CatalogArm]) -> None:
    materialized = tuple(arms)
    if len(materialized) != CATALOG_SIZE:
        raise R014Error(f"catalog must have {CATALOG_SIZE} arms")
    if len({arm.arm_id for arm in materialized}) != CATALOG_SIZE:
        raise R014Error("catalog arm ids are not unique")
    coordinates = {
        (
            arm.force_p_gain,
            arm.force_damping,
            arm.force_i_gain,
            arm.normal_filter_tau_s,
            arm.orientation_ko,
            arm.motion_kp,
        )
        for arm in materialized
    }
    if len(coordinates) != CATALOG_SIZE:
        raise R014Error("catalog has duplicate controller coordinates")
    for arm in materialized[:SOBOL_ARMS]:
        if not 1.25e-5 <= arm.p_over_d <= 4.0e-4:
            raise R014Error(f"P/D out of bounds: {arm.arm_id}")
        if not 7.0 <= arm.force_damping <= 224.0:
            raise R014Error(f"D out of bounds: {arm.arm_id}")
        if not 0.04375 <= arm.normal_filter_tau_s <= 0.0735784:
            raise R014Error(f"tau out of bounds: {arm.arm_id}")
        if not 0.05 <= arm.orientation_ko <= 0.8:
            raise R014Error(f"Ko out of bounds: {arm.arm_id}")
        if not 1.5 <= arm.motion_kp <= 6.0:
            raise R014Error(f"Kp out of bounds: {arm.arm_id}")
        if not 0.05 <= arm.i_over_p <= 0.5 or arm.force_i_gain > I_MAX + 1e-15:
            raise R014Error(f"I/P or I out of bounds: {arm.arm_id}")


def catalog_document() -> dict[str, Any]:
    arms = build_frozen_catalog()
    basis = {
        "schema": "step5d.qafs-bo/frozen-catalog-v1",
        "version": 1,
        "seed": FROZEN_CATALOG_SEED,
        "scramble": "scipy-owen",
        "arms": [arm.as_dict() for arm in arms],
        "historical_objectives_imported": False,
    }
    return {**basis, "catalog_sha256": sha256_value(basis)}


def normalized_log_coordinates(arm: CatalogArm) -> np.ndarray:
    """Return six log coordinates for deterministic maximin warm-start selection."""

    return np.asarray(
        [
            math.log(arm.p_over_d),
            math.log(arm.force_damping),
            math.log(arm.normal_filter_tau_s),
            math.log(arm.orientation_ko),
            math.log(arm.motion_kp),
            math.log(arm.i_over_p),
        ],
        dtype=float,
    )


@lru_cache(maxsize=None)
def deterministic_maximin_sobol(count: int = 6) -> tuple[str, ...]:
    if count < 1 or count > SOBOL_ARMS:
        raise R014Error("maximin count is outside the Sobol catalog")
    catalog = build_frozen_catalog()
    sobol = catalog[:SOBOL_ARMS]
    matrix = np.vstack([normalized_log_coordinates(arm) for arm in sobol])
    lower = matrix.min(axis=0)
    span = np.maximum(matrix.max(axis=0) - lower, 1e-12)
    matrix = (matrix - lower) / span
    center = np.full(6, 0.5)
    selected = [int(np.argmax(np.linalg.norm(matrix - center, axis=1)))]
    while len(selected) < count:
        distances = np.min(
            np.linalg.norm(matrix[:, None, :] - matrix[selected][None, :, :], axis=2),
            axis=1,
        )
        distances[selected] = -1.0
        selected.append(int(np.argmax(distances)))
    return tuple(sobol[index].arm_id for index in selected)
