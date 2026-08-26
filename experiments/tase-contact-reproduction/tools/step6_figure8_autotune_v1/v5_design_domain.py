"""Frozen six-dimensional V5 design domain and optimizer feature bounds.

The physical candidate is sampled in bounded controller coordinates.  The
optimizer consumes the corresponding base-2 log coordinates, so the bounds
must be explicit and shared by candidate validation, normalization, and fit
receipts.  Keeping this module dependency-free also lets hermetic tests and
the CUDA worker inspect the contract without importing the live robot stack.
"""

from __future__ import annotations

import math
from typing import Any


P_OVER_D_BOUNDS = (1.25e-5, 4.0e-4)
DAMPING_BOUNDS = (7.0, 224.0)
TAU_BOUNDS = (0.03094, 0.0735784)
KO_BOUNDS = (0.03536, 0.8)
MOTION_KP_BOUNDS = (1.5, 6.0)
I_OVER_P_BOUNDS = (0.05, 0.7071)
V5_DESIGN_DIMENSIONS = 6
V5_FEATURE_NAMES = (
    "log2_p_over_d",
    "log2_damping",
    "log2_tau_s",
    "log2_orientation_ko",
    "log2_motion_kp",
    "log2_i_over_p",
)


def _log2_bounds(bounds: tuple[float, float]) -> tuple[float, float]:
    return (math.log2(bounds[0]), math.log2(bounds[1]))


V5_FEATURE_BOUNDS = (
    _log2_bounds(P_OVER_D_BOUNDS),
    _log2_bounds(DAMPING_BOUNDS),
    _log2_bounds(TAU_BOUNDS),
    _log2_bounds(KO_BOUNDS),
    _log2_bounds(MOTION_KP_BOUNDS),
    _log2_bounds(I_OVER_P_BOUNDS),
)


def feature_bounds_receipt() -> dict[str, Any]:
    """Return a canonical, JSON-safe receipt for the fixed normalization box."""

    return {
        "schema": "step6.autotune/figure8-v5-design-bounds-v1",
        "version": 1,
        "dimensions": V5_DESIGN_DIMENSIONS,
        "feature_names": list(V5_FEATURE_NAMES),
        "feature_bounds_log2": [list(bounds) for bounds in V5_FEATURE_BOUNDS],
        "physical_bounds": {
            "p_over_d": list(P_OVER_D_BOUNDS),
            "damping": list(DAMPING_BOUNDS),
            "tau_s": list(TAU_BOUNDS),
            "orientation_ko": list(KO_BOUNDS),
            "motion_kp": list(MOTION_KP_BOUNDS),
            "i_over_p": list(I_OVER_P_BOUNDS),
        },
        "normalization": "fixed_design_bounds_not_observation_minmax",
    }


__all__ = [
    "DAMPING_BOUNDS",
    "I_OVER_P_BOUNDS",
    "KO_BOUNDS",
    "MOTION_KP_BOUNDS",
    "P_OVER_D_BOUNDS",
    "TAU_BOUNDS",
    "V5_DESIGN_DIMENSIONS",
    "V5_FEATURE_BOUNDS",
    "V5_FEATURE_NAMES",
    "feature_bounds_receipt",
]
