"""R013 six-dimensional I-on controller domain and executable lattice."""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Any, Mapping, Sequence


DOMAIN_SCHEMA = "step5d.autotune-v4/r013-fixed-six-dimensional-domain-v1"
MODEL_DIMENSIONS = (
    "log2_force_p_over_d",
    "log2_force_damping",
    "log2_normal_filter_tau_s",
    "log2_orientation_ko",
    "log2_motion_kp",
    "log2_force_i_gain_over_force_p_gain",
)
MODEL_DIMENSION_COUNT = 6
P_OVER_D_BOUNDS = (1.25e-5, 4.0e-4)
DAMPING_BOUNDS = (7.0, 224.0)
TAU_BOUNDS = (0.04375, 0.0735784)
KO_BOUNDS = (0.05, 0.8)
KP_BOUNDS = (1.5, 6.0)
KF_BOUNDS_S_INV = (0.05, 0.5)
BASELINE_KI_MAX = 0.003620386719675124
# Bounded continuation surface: this is the highest quarter-octave Ki that
# remains below the fixed 0.5 * P I-term authority for the reviewed probes.
# The runtime anti-windup authority clamp remains the non-tunable limit.
KI_MAX = BASELINE_KI_MAX
EXTENDED_KI_MAX = 0.008610779292198037
TARGET_FORCE_N = 5.0
KI_LATTICE_ANCHOR = 0.001810193359837562
LATTICE_STEP_OCTAVE = 0.25
DOMAIN_RAW_BOUNDS = {
    "p_over_d": P_OVER_D_BOUNDS,
    "force_damping": DAMPING_BOUNDS,
    "normal_filter_tau_s": TAU_BOUNDS,
    "orientation_ko": KO_BOUNDS,
    "motion_kp": KP_BOUNDS,
    "force_i_over_p": KF_BOUNDS_S_INV,
}
DOMAIN_LOG_BOUNDS = tuple(
    (math.log2(low), math.log2(high)) for low, high in DOMAIN_RAW_BOUNDS.values()
)
LATTICE_ANCHORS = {
    "force_p_gain": 0.0003535533906,
    "force_damping": 28.0,
    "force_i_gain": KI_LATTICE_ANCHOR,
    "normal_filter_tau_s": 0.35,
    "orientation_ko": 0.1,
    "motion_kp": 1.5,
}
SENTINEL_KI = (
    KI_LATTICE_ANCHOR * 2.0 ** -0.25,
    KI_LATTICE_ANCHOR,
    KI_LATTICE_ANCHOR * 2.0 ** 0.25,
)


class R013DomainError(ValueError):
    """A candidate is outside the complete R013 executable domain."""


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise R013DomainError(f"{name} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise R013DomainError(f"{name} must be numeric") from exc
    if not math.isfinite(result):
        raise R013DomainError(f"{name} must be finite")
    return result


def _number(candidate: Mapping[str, Any], key: str) -> float:
    if key not in candidate:
        raise R013DomainError(f"candidate lacks {key}")
    return _finite(candidate[key], key)


def nearest_lattice_value(
    value: float,
    *,
    anchor: float,
    bounds: tuple[float, float],
) -> float:
    parsed = _finite(value, "lattice value")
    if parsed <= 0.0 or bounds[0] <= 0.0 or bounds[1] < bounds[0]:
        raise R013DomainError("lattice value/bounds must be positive and ordered")
    levels = _lattice_levels(float(anchor), bounds)
    if not levels:
        raise R013DomainError("no quarter-octave lattice value lies inside bounds")
    return min(levels, key=lambda item: (abs(math.log2(item / parsed)), item))


@lru_cache(maxsize=None)
def _lattice_levels(anchor: float, bounds: tuple[float, float]) -> tuple[float, ...]:
    return tuple(
        anchor * 2.0 ** (step * LATTICE_STEP_OCTAVE)
        for step in range(-160, 161)
        if bounds[0] - 1e-15
        <= anchor * 2.0 ** (step * LATTICE_STEP_OCTAVE)
        <= bounds[1] + 1e-15
    )


def is_quarter_octave_lattice(value: float, anchor: float) -> bool:
    step = math.log2(_finite(value, "lattice value") / float(anchor)) / LATTICE_STEP_OCTAVE
    return math.isclose(step, round(step), rel_tol=0.0, abs_tol=1e-9)


def physical_candidate_key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    p = _number(candidate, "force_p_gain")
    damping = _number(candidate, "force_damping")
    i_gain = _number(candidate, "force_i_gain")
    tau = _number(candidate, "normal_filter_tau_s")
    ko = _number(candidate, "orientation_ko")
    kp = _number(candidate, "motion_kp")
    target = _finite(candidate.get("target_force_n", TARGET_FORCE_N), "target_force_n")
    i_off = candidate.get("i_off")
    if i_off not in (True, False):
        raise R013DomainError("R013 i_off must be boolean")
    if i_off and i_gain != 0.0:
        raise R013DomainError("R013 I-off candidate must have force_i_gain=0")
    if not i_off and i_gain <= 0.0:
        raise R013DomainError("R013 I-on candidate must have positive force_i_gain")
    if min(p, damping, tau, ko, kp) <= 0.0:
        raise R013DomainError("R013 physical coordinates must be positive")
    ratio = p / damping
    kf = i_gain / p if not i_off else 0.0
    checks = (
        P_OVER_D_BOUNDS[0] <= ratio <= P_OVER_D_BOUNDS[1],
        DAMPING_BOUNDS[0] <= damping <= DAMPING_BOUNDS[1],
        TAU_BOUNDS[0] <= tau <= TAU_BOUNDS[1],
        KO_BOUNDS[0] <= ko <= KO_BOUNDS[1],
        KP_BOUNDS[0] <= kp <= KP_BOUNDS[1],
        i_off or KF_BOUNDS_S_INV[0] <= kf <= KF_BOUNDS_S_INV[1],
        i_off or i_gain <= EXTENDED_KI_MAX + 1e-15,
        i_off or is_quarter_octave_lattice(i_gain, KI_LATTICE_ANCHOR),
        math.isclose(target, TARGET_FORCE_N, rel_tol=0.0, abs_tol=1e-12),
    )
    if not all(checks):
        raise R013DomainError("candidate violates the complete R013 6D domain")
    return (p, damping, i_gain, i_off, tau, ko, kp, target)


def candidate_to_log_features(candidate: Mapping[str, Any]) -> tuple[float, ...]:
    p, damping, i_gain, _i_off, tau, ko, kp, _target = physical_candidate_key(candidate)
    return (
        math.log2(p / damping),
        math.log2(damping),
        math.log2(tau),
        math.log2(ko),
        math.log2(kp),
        math.log2(i_gain / p),
    )


def candidate_to_normalized(candidate: Mapping[str, Any]) -> tuple[float, ...]:
    features = candidate_to_log_features(candidate)
    values = tuple(
        (value - low) / (high - low)
        for value, (low, high) in zip(features, DOMAIN_LOG_BOUNDS)
    )
    if any(value < -1e-10 or value > 1.0 + 1e-10 for value in values):
        raise R013DomainError("normalized candidate is outside [0,1]^6")
    return tuple(min(1.0, max(0.0, value)) for value in values)


def normalized_to_candidate(
    values: Sequence[float],
    *,
    template: Mapping[str, Any] | None = None,
    snap: bool = True,
) -> dict[str, Any]:
    if len(values) != MODEL_DIMENSION_COUNT:
        raise R013DomainError("R013 normalized point must have six dimensions")
    unit = tuple(_finite(value, "normalized coordinate") for value in values)
    if any(value < 0.0 or value > 1.0 for value in unit):
        raise R013DomainError("normalized coordinate is outside [0,1]")
    logs = tuple(
        low + value * (high - low)
        for value, (low, high) in zip(unit, DOMAIN_LOG_BOUNDS)
    )
    damping = 2.0 ** logs[1]
    p = (2.0 ** logs[0]) * damping
    candidate = {
        **dict(template or {}),
        "force_p_gain": p,
        "force_damping": damping,
        "force_i_gain": (2.0 ** logs[5]) * p,
        "i_off": False,
        "normal_filter_tau_s": 2.0 ** logs[2],
        "orientation_ko": 2.0 ** logs[3],
        "motion_kp": 2.0 ** logs[4],
        "target_force_n": TARGET_FORCE_N,
    }
    return snap_candidate_to_live_lattice(candidate) if snap else candidate


def snap_candidate_to_live_lattice(candidate: Mapping[str, Any]) -> dict[str, Any]:
    damping = nearest_lattice_value(
        _number(candidate, "force_damping"),
        anchor=LATTICE_ANCHORS["force_damping"],
        bounds=DAMPING_BOUNDS,
    )
    p = nearest_lattice_value(
        _number(candidate, "force_p_gain"),
        anchor=LATTICE_ANCHORS["force_p_gain"],
        bounds=(P_OVER_D_BOUNDS[0] * damping, P_OVER_D_BOUNDS[1] * damping),
    )
    i_bounds = (
        KF_BOUNDS_S_INV[0] * p,
        min(KF_BOUNDS_S_INV[1] * p, KI_MAX),
    )
    result = {
        **dict(candidate),
        "force_p_gain": p,
        "force_damping": damping,
        "force_i_gain": nearest_lattice_value(
            _number(candidate, "force_i_gain"),
            anchor=KI_LATTICE_ANCHOR,
            bounds=i_bounds,
        ),
        "i_off": False,
        "normal_filter_tau_s": nearest_lattice_value(
            _number(candidate, "normal_filter_tau_s"),
            anchor=LATTICE_ANCHORS["normal_filter_tau_s"],
            bounds=TAU_BOUNDS,
        ),
        "orientation_ko": nearest_lattice_value(
            _number(candidate, "orientation_ko"),
            anchor=LATTICE_ANCHORS["orientation_ko"],
            bounds=KO_BOUNDS,
        ),
        "motion_kp": nearest_lattice_value(
            _number(candidate, "motion_kp"),
            anchor=LATTICE_ANCHORS["motion_kp"],
            bounds=KP_BOUNDS,
        ),
        "target_force_n": TARGET_FORCE_N,
    }
    physical_candidate_key(result)
    return result


def candidate_supports_fixed_ki_seed(candidate: Mapping[str, Any], ki: float) -> bool:
    try:
        probe = {**dict(candidate), "force_i_gain": float(ki), "i_off": False}
        physical_candidate_key(probe)
    except (R013DomainError, TypeError, ValueError):
        return False
    return True


__all__ = [
    "DAMPING_BOUNDS", "DOMAIN_LOG_BOUNDS", "DOMAIN_RAW_BOUNDS", "DOMAIN_SCHEMA",
    "BASELINE_KI_MAX", "EXTENDED_KI_MAX", "KF_BOUNDS_S_INV", "KI_LATTICE_ANCHOR", "KI_MAX", "KO_BOUNDS", "KP_BOUNDS",
    "LATTICE_STEP_OCTAVE", "MODEL_DIMENSION_COUNT", "MODEL_DIMENSIONS",
    "P_OVER_D_BOUNDS", "R013DomainError", "SENTINEL_KI", "TARGET_FORCE_N", "TAU_BOUNDS",
    "candidate_supports_fixed_ki_seed", "candidate_to_log_features",
    "candidate_to_normalized", "is_quarter_octave_lattice", "nearest_lattice_value",
    "normalized_to_candidate", "physical_candidate_key", "snap_candidate_to_live_lattice",
]
